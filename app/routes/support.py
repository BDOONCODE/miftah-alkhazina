"""The general admin's page: support and integration. No company pages, IBANs or amounts."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from .. import throttle
from ..auth import LoginRequired, current_user, require_role
from ..db import get_session
from ..models import ReversalRequest, Role, SupportTicket, SupportTicketImage, TransferIssue, User
from ..routes.bank import _secret
from ..services import support as svc
from ..services.banking import is_simulated
from ..templating import templates
from ..web import flash, redirect

router = APIRouter()
admin_only = require_role(Role.ADMIN)


@router.get("/admin/support", response_class=HTMLResponse)
def support_page(request: Request, user: User = Depends(admin_only), session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "support/page.html", {
        "user": user,
        "companies": svc.integration_status(session),
        "issues": svc.issues(session),
        "requests": svc.requests(session),
        "tickets": svc.tickets(session),
        "TICKET_STATUS": svc.TICKET_STATUS,
        "LEG_LABELS": svc.LEG_LABELS,
        "REQUEST_STATUS": svc.REQUEST_STATUS,
    })


@router.post("/admin/issues/{issue_id}/resolve")
async def resolve_issue(
    request: Request, issue_id: int, user: User = Depends(admin_only), session: Session = Depends(get_session)
):
    issue = session.get(TransferIssue, issue_id)
    if issue is None:
        raise HTTPException(404, "التحويل غير موجود")
    form = await request.form()
    svc.resolve_issue(session, issue, user, str(form.get("note", "")))
    session.commit()
    flash(request, f"سُجّلت معالجة التحويل {issue.reference}")
    return redirect("/admin/support#issues")


@router.post("/admin/reversal-requests/{request_id}")
async def update_request(
    request: Request, request_id: int, user: User = Depends(admin_only), session: Session = Depends(get_session)
):
    row = session.get(ReversalRequest, request_id)
    if row is None:
        raise HTTPException(404, "الطلب غير موجود")
    form = await request.form()
    try:
        svc.update_request(session, row, user, str(form.get("status", "")), str(form.get("note", "")))
    except svc.SupportError as exc:
        flash(request, str(exc), "error")
        return redirect("/admin/support#requests")
    session.commit()
    flash(request, f"حُدّثت حالة طلب العكس #{row.id}")
    return redirect("/admin/support#requests")


@router.get("/admin/api", response_class=HTMLResponse)
def api_docs(request: Request, user: User = Depends(admin_only)):
    """The integration contract with the banking provider, for following up with Neotek."""
    return templates.TemplateResponse(request, "support/api.html", {
        "user": user,
        "webhook_on": bool(_secret()),
        "simulated": is_simulated(),
        "base_url": str(request.base_url).rstrip("/"),
    })


# ------------------------------------------------------------------ support tickets
# Open to signed-in users, and to a user whose account is disabled (identified at login, kept in the session)


def _ticket_sender(request: Request, session: Session) -> User | None:
    user_id = request.session.get("user_id") or request.session.get("disabled_user_id")
    return session.get(User, user_id) if user_id else None


def _ticket_page(request, session, sender, error=None, form=None, status_code=200):
    disabled = sender is not None and not sender.is_active
    return templates.TemplateResponse(request, "support/ticket.html", {
        "user": sender if sender is not None and not disabled else None,
        "sender": sender,
        "disabled": disabled,
        "error": error,
        "form": form or {},
        "mine": svc.my_tickets(session, sender) if sender is not None else [],
        "TICKET_STATUS": svc.TICKET_STATUS,
        "MAX_IMAGES": svc.MAX_IMAGES,
    }, status_code=status_code)


@router.get("/support", response_class=HTMLResponse)
def ticket_form(request: Request, session: Session = Depends(get_session)):
    sender = _ticket_sender(request, session)
    if sender is None:
        raise LoginRequired
    if sender.role is Role.ADMIN:
        return redirect("/admin/support#tickets")
    return _ticket_page(request, session, sender)


@router.post("/support")
async def submit_ticket(request: Request, session: Session = Depends(get_session)):
    sender = _ticket_sender(request, session)
    if sender is None:
        raise LoginRequired
    ip = request.client.host if request.client else "?"
    if throttle.is_blocked(ip, "support-ticket"):
        return _ticket_page(request, session, sender, "أرسلت طلبات كثيرة. حاول مرة أخرى بعد ١٠ دقائق", status_code=429)
    form = await request.form()
    images = [await f.read(svc.MAX_IMAGE_BYTES + 1) for f in form.getlist("images") if hasattr(f, "read")]
    fields = {k: str(form.get(k, "")) for k in ("subject", "body")}
    try:
        svc.open_ticket(session, user=sender, contact=sender.email or sender.username, images=images, **fields)
    except svc.SupportError as exc:
        return _ticket_page(request, session, sender, str(exc), fields, status_code=422)
    throttle.record_failure(ip, "support-ticket")  # counts submissions, so the form cannot be flooded
    session.commit()
    flash(request, "وصل طلبك إلى الدعم، وسيظهر الرد هنا")
    return redirect("/support")


@router.get("/admin/tickets/{ticket_id}", response_class=HTMLResponse)
def ticket_detail(
    request: Request, ticket_id: int, user: User = Depends(admin_only), session: Session = Depends(get_session)
):
    ticket = session.get(SupportTicket, ticket_id)
    if ticket is None:
        raise HTTPException(404, "الطلب غير موجود")
    svc.mark_opened(session, ticket, user)
    session.commit()
    return templates.TemplateResponse(request, "support/ticket_admin.html", {
        "user": user, "ticket": ticket, "TICKET_STATUS": svc.TICKET_STATUS,
    })


@router.get("/admin/ticket-images/{image_id}")
def ticket_image(image_id: int, user: User = Depends(admin_only), session: Session = Depends(get_session)):
    image = session.get(SupportTicketImage, image_id)
    if image is None:
        raise HTTPException(404, "لا توجد صورة")
    return Response(image.data, media_type=image.mime,
                    headers={"Content-Disposition": "inline", "X-Content-Type-Options": "nosniff"})


@router.post("/admin/tickets/{ticket_id}/close")
async def close_ticket(
    request: Request, ticket_id: int, user: User = Depends(admin_only), session: Session = Depends(get_session)
):
    ticket = session.get(SupportTicket, ticket_id)
    if ticket is None:
        raise HTTPException(404, "الطلب غير موجود")
    form = await request.form()
    try:
        svc.close_ticket(session, ticket, user, str(form.get("reply", "")))
    except svc.SupportError as exc:
        flash(request, str(exc), "error")
        return redirect(f"/admin/tickets/{ticket.id}")
    session.commit()
    flash(request, f"أُرسل الرد وأُغلق طلب الدعم #{ticket.id}")
    return redirect("/admin/support#tickets")
