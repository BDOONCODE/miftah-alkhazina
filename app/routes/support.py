"""The general admin's page: support and integration. No company pages, IBANs or amounts."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..auth import require_role
from ..db import get_session
from ..models import ReversalRequest, Role, TransferIssue, User
from ..services import support as svc
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
