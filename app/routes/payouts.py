from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..auth import current_user, get_entity_for
from ..db import get_session
from ..domain.periods import local_date
from ..models import BankAccount, User, utcnow
from ..services import payouts as svc
from ..services.banking import is_simulated
from ..services.policies import active_policy
from ..templating import templates

router = APIRouter()


@router.get("/entities/{entity_id}/balances", response_class=HTMLResponse)
def balances_page(
    request: Request, entity_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    """Held balance per item, when and to whom it transfers, and the transfer log. Read-only: transfers follow the schedule."""
    entity = get_entity_for(user, entity_id, session)
    today = local_date(utcnow())
    if policy := active_policy(session, entity_id):
        # Policies approved before the integration: open their items' accounts in the background
        svc.ensure_pool(session, entity_id)
        svc.ensure_virtual_accounts(session, entity_id, [pb.bucket_id for pb in policy.buckets])
    svc.run_due(session, entity_id, today)  # in case the server was asleep on the due date
    session.commit()
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
        },
    )
