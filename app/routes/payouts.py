from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..auth import current_user, get_entity_for, require_role
from ..db import get_session
from ..domain.money import format_amount
from ..domain.periods import local_date
from ..models import BankAccount, PayoutStatus, Role, User, utcnow
from ..services import payouts as svc
from ..services.banking import is_simulated
from ..services.policies import active_policy
from ..templating import templates
from ..web import flash, redirect

router = APIRouter()
editor = require_role(Role.ACCOUNTANT)


@router.post("/entities/{entity_id}/surplus/transfer-now")
def transfer_surplus_now(
    request: Request, entity_id: int, user: User = Depends(editor), session: Session = Depends(get_session)
):
    """Pay the surplus now, to the account set in the policy, instead of waiting for its schedule."""
    entity = get_entity_for(user, entity_id, session)
    try:
        sent = svc.transfer_surplus_now(session, entity, user)
    except svc.PayoutError as exc:
        session.rollback()
        flash(request, str(exc), "error")
        return redirect(f"/entities/{entity_id}/balances")
    session.commit()
    failed = [p for p in sent if p.status is not PayoutStatus.SENT]
    if failed:
        flash(request, f"تعذّر التحويل: {failed[0].error}", "error")
    else:
        flash(request, f"حُوِّل {format_amount(sum(p.amount for p in sent))} ر.س إلى «{sent[0].beneficiary_name}»")
    return redirect(f"/entities/{entity_id}/balances")


@router.get("/entities/{entity_id}/balances", response_class=HTMLResponse)
def balances_page(
    request: Request, entity_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    """Held balance per item, when and to whom it transfers, and the transfer log. Read-only: transfers follow the schedule."""
    entity = get_entity_for(user, entity_id, session)
    today = local_date(utcnow())
    svc.run_due(session, entity_id, today)  # in case the server was asleep on the due date
    session.commit()
    policy = active_policy(session, entity_id)
    views = svc.held_balances(session, entity_id, today)
    return templates.TemplateResponse(
        request,
        "payouts/page.html",
        {
            "user": user,
            "entity": entity,
            "tab": "balances",
            "items": views,
            "held_total": sum(v.balance for v in views),
            "surplus_total": svc.surplus_balance(session, entity_id),
            "surplus_to": svc.surplus_account(session, entity_id, policy.surplus_account_id) if policy else None,
            "payouts": svc.recent_payouts(session, entity_id),
            "names": {**{v.bucket_id: v.name for v in views}, entity.surplus_bucket_id: svc.SURPLUS_NAME},
            "simulated": is_simulated(),
        },
    )
