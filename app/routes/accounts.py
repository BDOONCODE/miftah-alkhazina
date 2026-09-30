from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..auth import current_user, get_entity_for, require_role
from ..db import get_session
from ..models import BankAccount, Role, User
from ..services import accounts as svc
from ..templating import templates
from ..web import flash, redirect

router = APIRouter()
editor = require_role(Role.ACCOUNTANT, Role.ADMIN)


@router.get("/entities/{entity_id}/accounts", response_class=HTMLResponse)
def accounts_page(
    request: Request, entity_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    entity = get_entity_for(user, entity_id, session)
    return templates.TemplateResponse(
        request,
        "accounts/list.html",
        {
            "user": user,
            "entity": entity,
            "tab": "accounts",
            "accounts": svc.accounts_for(session, entity_id),
            "has_pool": svc.pool_account(session, entity_id) is not None,
            "KINDS": svc.KIND_LABELS,
            "HINTS": svc.KIND_HINTS,
        },
    )


@router.post("/entities/{entity_id}/accounts")
async def add_account(request: Request, entity_id: int, user: User = Depends(editor), session: Session = Depends(get_session)):
    entity = get_entity_for(user, entity_id, session)
    form = await request.form()
    try:
        account = svc.add_account(
            session,
            entity,
            user,
            name=str(form.get("name", "")),
            bank_name=str(form.get("bank_name", "")),
            iban=str(form.get("iban", "")),
            kind=str(form.get("kind", "")),
        )
    except svc.AccountError as exc:
        session.rollback()
        flash(request, str(exc), "error")
        return redirect(f"/entities/{entity_id}/accounts")
    session.commit()
    flash(request, f"انضاف الحساب «{account.name}»")
    return redirect(f"/entities/{entity_id}/accounts")


@router.post("/accounts/{account_id}/toggle")
def toggle_account(request: Request, account_id: int, user: User = Depends(editor), session: Session = Depends(get_session)):
    account = session.get(BankAccount, account_id)
    if account is None:
        raise HTTPException(404, "الحساب غير موجود")
    get_entity_for(user, account.entity_id, session)
    try:
        svc.toggle_account(session, account, user)
    except svc.AccountError as exc:
        session.rollback()
        flash(request, str(exc), "error")
        return redirect(f"/entities/{account.entity_id}/accounts")
    session.commit()
    return redirect(f"/entities/{account.entity_id}/accounts")
