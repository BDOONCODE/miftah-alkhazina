from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import mailer
from ..auth import current_user, get_entity_for, require_role
from ..db import get_session
from ..models import Role, User
from ..services import companies as svc
from ..services.policies import PolicyError
from ..services.policies import set_approval_mode as set_mode
from ..templating import templates
from ..web import flash, redirect

router = APIRouter()
accountant = require_role(Role.ACCOUNTANT)

COMPANY_FIELDS = ["name", "cr_number", "vat_number", "bank_name", "iban", "contact_name", "contact_phone"]


def _company_input(form) -> svc.CompanyInput:
    return svc.CompanyInput(**{f: str(form.get(f, "")) for f in COMPANY_FIELDS})


def _reviewer_input(form) -> svc.ReviewerInput:
    return svc.ReviewerInput(
        full_name=str(form.get("reviewer_full_name", "")),
        email=str(form.get("reviewer_email", "")),
    )


def _invite(request: Request, reviewer: User, entity) -> str:
    """Email the invite when email is set up; the link is also shown on the company page to copy."""
    from ..routes.signup import _base_url
    from ..services import signup

    sent = signup.send_invite(reviewer, _base_url(request), entity.name) if mailer.is_configured() else False
    return "وأُرسل إلى بريده أيضًا" if sent else "انسخه من قائمة المراجعين في «بيانات الشركة» وأرسله إليه"


def _invite_links(request: Request, entity) -> dict[int, str]:
    from ..routes.signup import _base_url
    from ..services import signup

    return {r.id: signup.invite_link(r, _base_url(request)) for r in entity.reviewers if not r.email_verified}


@router.get("/companies/new", response_class=HTMLResponse)
def new_company_form(request: Request, user: User = Depends(accountant)):
    return templates.TemplateResponse(request, "companies/new.html", {"user": user, "form": {}, "error": None})


@router.post("/companies/new")
async def register_company(request: Request, user: User = Depends(accountant), session: Session = Depends(get_session)):
    form = await request.form()
    try:
        mode = str(form.get("approval_mode", "reviewer"))
        reviewer = _reviewer_input(form) if mode == "reviewer" else None
        entity = svc.register_company(session, user, _company_input(form), reviewer, mode)
    except svc.CompanyError as exc:
        session.rollback()
        values = dict(form.items())
        return templates.TemplateResponse(
            request, "companies/new.html", {"user": user, "form": values, "error": str(exc)}, status_code=422
        )
    session.commit()
    invited = next((r for r in entity.reviewers), None)
    note = f" دُعي المراجع «{invited.full_name}»، {_invite(request, invited, entity)}." if invited else ""
    flash(request, f"تم تسجيل «{entity.name}».{note} الخطوة التالية: أضف حسابات الشركة البنكية، ثم سياسة التقسيم")
    return redirect(f"/entities/{entity.id}/accounts")


@router.get("/entities/{entity_id}/settings", response_class=HTMLResponse)
def company_settings(
    request: Request, entity_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    entity = get_entity_for(user, entity_id, session)
    return templates.TemplateResponse(
        request, "companies/settings.html",
        {"user": user, "entity": entity, "tab": "settings", "error": None, "invite_links": _invite_links(request, entity)}
    )


@router.post("/entities/{entity_id}/settings")
async def update_company(
    request: Request, entity_id: int, user: User = Depends(accountant), session: Session = Depends(get_session)
):
    entity = get_entity_for(user, entity_id, session)
    form = await request.form()
    try:
        svc.update_company(session, entity, _company_input(form), user)
    except svc.CompanyError as exc:
        session.rollback()
        flash(request, str(exc), "error")
        return redirect(f"/entities/{entity_id}/settings")
    session.commit()
    flash(request, "تم حفظ بيانات الشركة")
    return redirect(f"/entities/{entity_id}/settings")


@router.post("/entities/{entity_id}/reviewers")
async def add_reviewer(
    request: Request, entity_id: int, user: User = Depends(accountant), session: Session = Depends(get_session)
):
    entity = get_entity_for(user, entity_id, session)
    form = await request.form()
    try:
        reviewer = svc.add_reviewer(session, entity, _reviewer_input(form), user)
    except svc.CompanyError as exc:
        session.rollback()
        flash(request, str(exc), "error")
        return redirect(f"/entities/{entity_id}/settings")
    session.commit()
    flash(request, f"تم إنشاء رابط دعوة «{reviewer.full_name}». {_invite(request, reviewer, entity)}")
    return redirect(f"/entities/{entity_id}/settings")


@router.post("/entities/{entity_id}/approval-mode")
async def set_approval_mode(
    request: Request, entity_id: int, user: User = Depends(accountant), session: Session = Depends(get_session)
):
    entity = get_entity_for(user, entity_id, session)
    form = await request.form()
    try:
        set_mode(session, entity, user, str(form.get("approval_mode", "")))
    except PolicyError as exc:
        session.rollback()
        flash(request, str(exc), "error")
        return redirect(f"/entities/{entity_id}/settings")
    session.commit()
    flash(request, "تم تغيير طريقة اعتماد السياسات")
    return redirect(f"/entities/{entity_id}/settings")
