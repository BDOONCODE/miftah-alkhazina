from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..auth import current_user, get_entity_for, require_role
from ..db import get_session
from ..models import Role, User
from ..services import companies as svc
from ..services.policies import PolicyError
from ..services.policies import set_approval_mode as set_mode
from ..templating import templates
from ..web import flash, redirect

router = APIRouter()
accountant = require_role(Role.ACCOUNTANT, Role.ADMIN)

COMPANY_FIELDS = ["name", "cr_number", "vat_number", "bank_name", "iban", "contact_name", "contact_phone"]


def _company_input(form) -> svc.CompanyInput:
    return svc.CompanyInput(**{f: str(form.get(f, "")) for f in COMPANY_FIELDS})


def _reviewer_input(form) -> svc.ReviewerInput:
    return svc.ReviewerInput(
        full_name=str(form.get("reviewer_full_name", "")),
        username=str(form.get("reviewer_username", "")),
        password=str(form.get("reviewer_password", "")),
    )


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
        values = {k: v for k, v in form.items() if k != "reviewer_password"}
        return templates.TemplateResponse(
            request, "companies/new.html", {"user": user, "form": values, "error": str(exc)}, status_code=422
        )
    session.commit()
    next_step = "حدد سياسة التقسيم واعتمدها" if entity.approval_mode == "self" else "حدد سياسة التقسيم وأرسلها للمراجع"
    flash(request, f"تم تسجيل «{entity.name}». الخطوة الجاية: {next_step}")
    return redirect(f"/entities/{entity.id}/policy")


@router.get("/entities/{entity_id}/settings", response_class=HTMLResponse)
def company_settings(
    request: Request, entity_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    entity = get_entity_for(user, entity_id, session)
    return templates.TemplateResponse(
        request, "companies/settings.html", {"user": user, "entity": entity, "tab": "settings", "error": None}
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
    flash(request, "انحفظت بيانات الشركة")
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
    flash(request, f"أُضيف المراجع «{reviewer.full_name}». أعطه اسم المستخدم وكلمة المرور المؤقتة")
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
    flash(request, "تغيّرت طريقة اعتماد السياسات")
    return redirect(f"/entities/{entity_id}/settings")
