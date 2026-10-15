from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..auth import current_user, get_entity_for, require_role
from ..db import get_session
from ..domain.money import format_amount, parse_amount
from ..domain.periods import local_date
from ..models import BankAccount, PayoutStatus, Role, User, utcnow
from ..services import payouts as svc
from ..services.accounts import INCOME_KINDS, accounts_for
from ..services.banking import is_simulated
from ..services.policies import active_policy
from ..templating import templates
from ..web import flash, redirect

router = APIRouter()
editor = require_role(Role.ACCOUNTANT)


@router.post("/entities/{entity_id}/withdraw")
async def withdraw(request: Request, entity_id: int, user: User = Depends(editor), session: Session = Depends(get_session)):
    """Take money back from the set-aside surplus, to one of the company's own registered accounts."""
    entity = get_entity_for(user, entity_id, session)
    form = await request.form()
    try:
        amount = parse_amount(str(form.get("amount", "")))
        account_id = str(form.get("account_id", ""))
        payout = svc.withdraw(session, entity, user, amount, int(account_id) if account_id.isdigit() else 0)
    except (ValueError, svc.PayoutError) as exc:
        session.rollback()
        flash(request, str(exc), "error")
        return redirect(f"/entities/{entity_id}/balances")
    session.commit()
    if payout.status is PayoutStatus.SENT:
        flash(request, f"حُوِّل {format_amount(payout.amount)} ر.س إلى «{payout.beneficiary_name}»")
    else:
        flash(request, f"تعذّر التحويل: {payout.error}", "error")
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
            "pool_total": svc.pool_balance(session, entity_id),
            "isolated_total": svc.isolated_surplus(session, entity_id),
            "surplus_to": session.get(BankAccount, policy.surplus_account_id) if policy and policy.surplus_account_id else None,
            "payouts": svc.recent_payouts(session, entity_id),
            "names": {**{v.bucket_id: v.name for v in views}, entity.surplus_bucket_id: svc.SURPLUS_NAME},
            "simulated": is_simulated(),
            "own_accounts": [a for a in accounts_for(session, entity_id, active_only=True) if a.kind in INCOME_KINDS],
        },
    )
