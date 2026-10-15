from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import current_user, get_entity_for, require_role
from ..db import get_session
from ..domain.money import parse_amount
from ..domain.periods import RIYADH
from ..models import (
    AccountKind, BankAccount, HeldDeposit, Payout, PayoutStatus, PoolMove, PolicyBucket, Role, Transaction, User, utcnow,
)
from ..services import payouts as payout_svc
from ..services import support
from ..services import transactions as svc
from ..services import virtual_ibans
from ..services.accounts import accounts_for
from ..services.policies import active_policy
from ..templating import templates
from ..web import flash, redirect

router = APIRouter()
INCOMING_KINDS = (AccountKind.SOURCE, AccountKind.POOL)
editor = require_role(Role.ACCOUNTANT)
PAGE_SIZE = 50


def _bucket_names(session: Session, txns: list[Transaction]) -> dict[int, str]:
    """Each item's name as it was in the policy that split the transaction."""
    policy_ids = {t.policy_id for t in txns}
    names: dict[int, str] = {}
    if policy_ids:
        for pb in session.scalars(select(PolicyBucket).where(PolicyBucket.policy_id.in_(policy_ids))):
            names.setdefault(pb.bucket_id, pb.name)
    return names


def _now_local() -> str:
    return utcnow().astimezone(RIYADH).strftime("%Y-%m-%dT%H:%M")


@router.get("/entities/{entity_id}/transactions", response_class=HTMLResponse)
def transactions_page(
    request: Request, entity_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    entity = get_entity_for(user, entity_id, session)
    deposit_iban = virtual_ibans.current(session, entity)
    session.commit()
    txns = list(
        session.scalars(
            select(Transaction).where(Transaction.entity_id == entity_id).order_by(Transaction.id.desc()).limit(PAGE_SIZE)
        )
    )
    reversed_ids = {t.reversal_of for t in txns if t.reversal_of}
    reversible = svc.reversible_transaction(session, entity_id)
    return templates.TemplateResponse(
        request,
        "transactions/list.html",
        {
            "user": user,
            "entity": entity,
            "tab": "transactions",
            "txns": txns,
            "reversed_ids": reversed_ids,
            "reversible_id": reversible.id if reversible else None,
            "policy": active_policy(session, entity_id),
            "now": _now_local(),
            "names": _bucket_names(session, txns),
            "form": {},
            "sources": [a for a in accounts_for(session, entity_id, active_only=True) if a.kind in INCOMING_KINDS],
            "account_names": {a.id: a.name for a in accounts_for(session, entity_id)},
            "held": svc.held_deposits(session, entity_id),
            "deposit_iban": deposit_iban,
        },
    )


@router.post("/entities/{entity_id}/transactions")
async def add_transaction(
    request: Request, entity_id: int, user: User = Depends(editor), session: Session = Depends(get_session)
):
    entity = get_entity_for(user, entity_id, session)
    form = await request.form()
    try:
        amount = parse_amount(str(form.get("amount", "")))
        occurred_at = datetime.fromisoformat(str(form.get("occurred_at", ""))).replace(tzinfo=RIYADH)
    except ValueError as exc:
        flash(request, f"تحقق من المبلغ والتاريخ: {exc}", "error")
        return redirect(f"/entities/{entity_id}/transactions")
    if occurred_at > utcnow() + timedelta(minutes=5):
        flash(request, "لا يمكن أن يكون تاريخ المعاملة في المستقبل", "error")
        return redirect(f"/entities/{entity_id}/transactions")
    try:
        source_id = str(form.get("source_account_id", ""))
        txn = svc.record_transaction(
            session, entity, user, amount, occurred_at, str(form.get("note", "")),
            source_account_id=int(source_id) if source_id.isdigit() else None,
        )
    except svc.TransactionError as exc:
        session.rollback()
        payout_svc.record_failure(session, exc)
        flash(request, str(exc), "error")
        return redirect(f"/entities/{entity_id}/transactions")
    session.commit()
    return redirect(f"/transactions/{txn.id}")


@router.post("/entities/{entity_id}/bank-simulator")
async def simulate_bank_credit(
    request: Request, entity_id: int, user: User = Depends(editor), session: Session = Depends(get_session)
):
    """Simulated deposit from the bank: goes through exactly the same path as a real bank notification."""
    entity = get_entity_for(user, entity_id, session)
    form = await request.form()
    wants_json = "application/json" in request.headers.get("accept", "")
    account_id = str(form.get("account_id", ""))
    # "other": a transfer from an account the company has not registered (it waits for the accountant)
    account = session.get(BankAccount, int(account_id)) if account_id.isdigit() else None
    deposit_iban = virtual_ibans.for_entity(session, entity.id)
    error = None
    if deposit_iban is None:
        error = "لم يُخصَّص آيبان الإيداع لهذه الشركة بعد"
    elif account_id != "other" and (account is None or account.entity_id != entity.id or account.kind not in INCOMING_KINDS):
        error = "اختر الحساب الذي حُوِّل منه المبلغ"
    else:
        try:
            raw = str(form.get("amount", "")).strip()
            # No amount: a realistic random amount between 500 and 15,000 riyals
            amount = parse_amount(raw) if raw else random.randint(500, 15_000) * 100
            sender = account.iban if account else f"SA65{random.randint(10**19, 10**20 - 1):020d}"
            txn = svc.ingest_bank_credit(
                session, deposit_iban.iban, sender, amount, utcnow(), f"SIM-{uuid.uuid4().hex[:12]}",
                sender_name=account.name if account else "حساب غير مسجّل", note="إيداع تجريبي (محاكاة البنك)",
            )
        except ValueError as exc:  # includes TransactionError
            error = str(exc)
    if error:
        session.rollback()
        if wants_json:
            return JSONResponse({"error": error}, status_code=422)
        flash(request, error, "error")
        return redirect(f"/entities/{entity_id}/transactions")
    session.commit()
    if isinstance(txn, HeldDeposit):
        if wants_json:
            return JSONResponse({"held_id": txn.id, "amount": txn.amount / 100})
        flash(request, "وصلت حوالة من حساب غير مسجّل، وتنتظر تأكيدك أدناه")
        return redirect(f"/entities/{entity_id}/transactions")
    if wants_json:
        return JSONResponse({"transaction_id": txn.id, "amount": txn.amount / 100, "surplus": txn.surplus / 100})
    flash(request, f"وصل إيداع من «{account.name}» وقُسِّم تلقائيًا")
    return redirect(f"/transactions/{txn.id}")


@router.post("/held-deposits/{held_id}/{decision}")
async def decide_held(
    request: Request, held_id: int, decision: str, user: User = Depends(editor), session: Session = Depends(get_session)
):
    """The accountant confirms a transfer from an unregistered account (split now), or refuses it (returned)."""
    held = session.get(HeldDeposit, held_id)
    if held is None or decision not in ("accept", "reject"):
        raise HTTPException(404, "الحوالة غير موجودة")
    get_entity_for(user, held.entity_id, session)
    form = await request.form()
    try:
        if decision == "accept":
            txn = svc.accept_held(session, held, user, register=form.get("register") == "1")
        else:
            svc.reject_held(session, held, user)
    except svc.TransactionError as exc:
        session.rollback()
        flash(request, str(exc), "error")
        return redirect(f"/entities/{held.entity_id}/transactions")
    session.commit()
    if decision == "accept":
        flash(request, "أُكِّدت الحوالة وقُسِّمت")
        return redirect(f"/transactions/{txn.id}")
    flash(request, "رُفضت الحوالة، وسيتابع الدعم إرجاعها إلى المرسل")
    return redirect(f"/entities/{held.entity_id}/transactions")


def _txn_for(user: User, txn_id: int, session: Session) -> Transaction:
    txn = session.get(Transaction, txn_id)
    if txn is None:
        raise HTTPException(404, "المعاملة غير موجودة")
    get_entity_for(user, txn.entity_id, session)
    return txn


@router.get("/transactions/{txn_id}", response_class=HTMLResponse)
def transaction_detail(
    request: Request, txn_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    txn = _txn_for(user, txn_id, session)
    reversible = svc.reversible_transaction(session, txn.entity_id)
    reversal = session.scalar(select(Transaction).where(Transaction.reversal_of == txn.id))
    return templates.TemplateResponse(
        request,
        "transactions/detail.html",
        {
            "user": user,
            "entity": get_entity_for(user, txn.entity_id, session),
            "tab": "transactions",
            "txn": txn,
            "names": _bucket_names(session, [txn]),
            "can_reverse": reversible is not None and reversible.id == txn.id,
            "reversal": reversal,
            "original": session.get(Transaction, txn.reversal_of) if txn.reversal_of else None,
            "source_account": session.get(BankAccount, txn.source_account_id) if txn.source_account_id else None,
            "payouts": list(session.scalars(select(Payout).where(Payout.transaction_id == txn.id).order_by(Payout.id))),
            "reversal_requests": support.requests_for(session, txn.id),
            "REQUEST_STATUS": support.REQUEST_STATUS,
            "covers": list(session.scalars(
                select(PoolMove).where(PoolMove.transaction_id == txn.id, PoolMove.kind == "cover").order_by(PoolMove.id)
            )),
        },
    )


@router.post("/transactions/{txn_id}/reverse")
async def reverse(request: Request, txn_id: int, user: User = Depends(editor), session: Session = Depends(get_session)):
    txn = _txn_for(user, txn_id, session)
    form = await request.form()
    try:
        reversal = svc.reverse_transaction(session, txn, user, str(form.get("reason", "")))
    except svc.TransactionError as exc:
        session.rollback()
        payout_svc.record_failure(session, exc)
        flash(request, str(exc), "error")
        return redirect(f"/transactions/{txn_id}")
    session.commit()
    refund = session.scalar(select(Payout).where(Payout.transaction_id == reversal.id, Payout.reason == "refund"))
    if refund is None:
        flash(request, "عُكست المعاملة وأُعيدت أرصدة البنود إلى ما كانت عليه قبلها")
    elif refund.status is PayoutStatus.SENT:
        flash(request, f"عُكست المعاملة وأُرجع المبلغ إلى «{refund.beneficiary_name}»")
    else:
        flash(request, f"عُكست المعاملة، وتعذّر إرجاع المبلغ: {refund.error}. سيتابعه الدعم", "error")
    return redirect(f"/transactions/{reversal.id}")


@router.post("/transactions/{txn_id}/reversal-request")
async def request_reversal(
    request: Request, txn_id: int, user: User = Depends(editor), session: Session = Depends(get_session)
):
    """Money already left to a beneficiary: the accountant asks support to reverse it with the bank."""
    txn = _txn_for(user, txn_id, session)
    form = await request.form()
    try:
        support.request_reversal(session, txn, user, str(form.get("reason", "")))
    except support.SupportError as exc:
        flash(request, str(exc), "error")
        return redirect(f"/transactions/{txn_id}")
    session.commit()
    flash(request, "رُفع طلب العكس إلى الدعم، وستظهر حالته هنا")
    return redirect(f"/transactions/{txn_id}")
