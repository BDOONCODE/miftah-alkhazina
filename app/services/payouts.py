"""Transferring item balances to beneficiaries according to their schedule.

Flow: income lands in the pool account -> is split -> each item's share is held in its own virtual account
(at the banking provider, in the background; users never deal with it) -> and is transferred to the beneficiary:
  - an item with no due date (immediate): transfers as soon as it is split.
  - an item with a due date: accumulates and transfers in full on the due date (first day after the cycle ends).
The surplus goes to the account the accountant chose, or stays in the pool.

Held balance of an item = total allocated to it - total successful transfers from it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..domain.periods import period_bounds, period_key
from ..domain.types import AllocationLine, Frequency
from ..models import (
    AccountKind,
    AllocationEvent,
    AuditLog,
    BankAccount,
    Bucket,
    BucketPeriodState,
    Entity,
    Payout,
    PayoutStatus,
    Policy,
    PolicyBucket,
    Transaction,
)
from .accounts import DESTINATION_KINDS
from .banking import BankingError, provider

REASON_LABELS = {"immediate": "فور التقسيم", "due": "يوم الاستحقاق", "surplus": "الفائض"}


class PayoutError(RuntimeError):
    """A money movement at the banking provider failed."""


def _lock_entity(session: Session, entity_id: int) -> None:
    session.scalars(select(Entity).where(Entity.id == entity_id).with_for_update()).one()


# ------------------------------------------------------------------ virtual accounts (background)


def ensure_pool(session: Session, entity_id: int) -> str:
    """The company's pool virtual account (opened once). Returns its provider id."""
    entity = session.get(Entity, entity_id)
    if entity.pool_virtual_ref is None:
        entity.pool_virtual_iban, entity.pool_virtual_ref = provider().open_pool_account(entity_id, entity.name)
        session.add(AuditLog(entity_id=entity_id, action="virtual_account.pool_opened", details={}))
    return entity.pool_virtual_ref


def fund_items(session: Session, entity_id: int, txn: Transaction, source: BankAccount | None) -> None:
    """Move an incoming amount through the provider: company account -> pool -> each item's virtual account.

    The surplus is simply what stays in the pool. A provider failure aborts the whole transaction,
    so the bank can resend the notification and nothing is half-split.
    """
    pool = ensure_pool(session, entity_id)
    try:
        if source is not None:
            txn.sweep_ref = provider().sweep(
                from_iban=source.iban, to_ref=pool, amount=txn.amount, reference=f"TX-{txn.id}-sweep"
            )
        events = [e for e in txn.events if e.allocated > 0]
        ensure_virtual_accounts(session, entity_id, [e.bucket_id for e in events])
        for event in events:
            bucket = session.get(Bucket, event.bucket_id)
            event.move_ref = provider().move(
                from_ref=pool, to_ref=bucket.virtual_ref, amount=event.allocated, reference=f"TX-{txn.id}-B{bucket.id}"
            )
    except BankingError as exc:
        raise PayoutError(f"تعذّر تنفيذ العملية لدى البنك: {exc}") from exc


def unwind_items(session: Session, original: Transaction, reversal: Transaction, source: BankAccount | None) -> None:
    """Reverse the provider moves of a transaction: item accounts -> pool -> back to the company account."""
    pool = ensure_pool(session, original.entity_id)
    try:
        for event in original.events:
            if event.allocated > 0 and event.move_ref:
                bucket = session.get(Bucket, event.bucket_id)
                provider().move(
                    from_ref=bucket.virtual_ref, to_ref=pool, amount=event.allocated,
                    reference=f"TX-{reversal.id}-B{bucket.id}",
                )
        if original.sweep_ref and source is not None:
            reversal.sweep_ref = provider().transfer(
                from_ref=pool, to_iban=source.iban, amount=original.amount, reference=f"TX-{reversal.id}-sweep"
            )
    except BankingError as exc:
        raise PayoutError(f"تعذّر تنفيذ العكس لدى البنك: {exc}") from exc


def pool_balance(session: Session, entity_id: int) -> int:
    """What stays in the pool: all surplus minus the surplus already transferred out."""
    surplus = session.scalar(
        select(func.coalesce(func.sum(Transaction.surplus), 0)).where(Transaction.entity_id == entity_id)
    )
    paid = session.scalar(
        select(func.coalesce(func.sum(Payout.amount), 0)).where(
            Payout.entity_id == entity_id, Payout.bucket_id.is_(None), Payout.status == PayoutStatus.SENT
        )
    )
    return int(surplus or 0) - int(paid or 0)


def ensure_virtual_accounts(session: Session, entity_id: int, bucket_ids: list[int]) -> None:
    """Every item in an approved policy has a virtual account at the provider (opened once, kept with the item)."""
    names = {
        pb.bucket_id: pb.name
        for pb in session.scalars(select(PolicyBucket).where(PolicyBucket.bucket_id.in_(bucket_ids)))
    }
    for bucket in session.scalars(select(Bucket).where(Bucket.id.in_(bucket_ids), Bucket.virtual_ref.is_(None))):
        bucket.virtual_iban, bucket.virtual_ref = provider().open_virtual_account(
            entity_id, bucket.id, names.get(bucket.id, "")
        )
        session.add(
            AuditLog(
                entity_id=entity_id,
                action="virtual_account.opened",
                details={"bucket_id": bucket.id, "name": names.get(bucket.id, "")},
            )
        )


def balances(session: Session, entity_id: int) -> dict[int, int]:
    """Held balance per item (halalas)."""
    session.flush()
    allocated = session.execute(
        select(AllocationEvent.bucket_id, func.sum(AllocationEvent.allocated))
        .join(Transaction, Transaction.id == AllocationEvent.transaction_id)
        .where(Transaction.entity_id == entity_id)
        .group_by(AllocationEvent.bucket_id)
    )
    result = {bucket_id: int(total or 0) for bucket_id, total in allocated}
    paid = session.execute(
        select(Payout.bucket_id, func.sum(Payout.amount))
        .where(Payout.entity_id == entity_id, Payout.status == PayoutStatus.SENT, Payout.bucket_id.is_not(None))
        .group_by(Payout.bucket_id)
    )
    for bucket_id, total in paid:
        result[bucket_id] = result.get(bucket_id, 0) - int(total or 0)
    return result


def _allocated_in_period(session: Session, bucket_id: int, key: str) -> int:
    return int(
        session.scalar(
            select(func.coalesce(func.sum(AllocationEvent.allocated), 0)).where(
                AllocationEvent.bucket_id == bucket_id, AllocationEvent.period_key == key
            )
        )
        or 0
    )


# ------------------------------------------------------------------ transfers


def _send(
    session: Session,
    entity_id: int,
    bucket_id: int | None,
    amount: int,
    beneficiary: BankAccount,
    reason: str,
    *,
    transaction_id: int | None = None,
    period: str | None = None,
) -> Payout:
    if bucket_id is not None:
        bucket = session.get(Bucket, bucket_id)
        if bucket.virtual_ref is None:
            ensure_virtual_accounts(session, entity_id, [bucket_id])
        from_ref = bucket.virtual_ref
    else:
        from_ref = ensure_pool(session, entity_id)  # the surplus leaves from the pool account itself
    payout = Payout(
        entity_id=entity_id,
        bucket_id=bucket_id,
        amount=amount,
        reason=reason,
        period_key=period,
        transaction_id=transaction_id,
        beneficiary_account_id=beneficiary.id,
        beneficiary_name=beneficiary.name,
        beneficiary_iban=beneficiary.iban,
        reference=f"PO-{uuid.uuid4().hex[:16]}",
    )
    try:
        payout.provider_ref = provider().transfer(
            from_ref=from_ref, to_iban=beneficiary.iban, amount=amount, reference=payout.reference
        )
        payout.status = PayoutStatus.SENT
    except BankingError as exc:
        payout.status, payout.error = PayoutStatus.FAILED, str(exc)
    session.add(payout)
    session.flush()
    session.add(
        AuditLog(
            entity_id=entity_id,
            action="payout.sent" if payout.status is PayoutStatus.SENT else "payout.failed",
            details={"payout_id": payout.id, "bucket_id": bucket_id, "amount": amount, "reason": reason,
                     "to": beneficiary.name, "error": payout.error},
        )
    )
    return payout


def _beneficiary(session: Session, entity_id: int, account_id: int | None) -> BankAccount | None:
    account = session.get(BankAccount, account_id) if account_id else None
    if account is None or account.entity_id != entity_id or not account.is_active or account.kind not in DESTINATION_KINDS:
        return None
    return account


def pay_on_allocation(
    session: Session, entity_id: int, policy: Policy, txn: Transaction, lines: tuple[AllocationLine, ...], surplus: int
) -> list[Payout]:
    """Right after the split: items with no due date transfer, and the surplus goes to its account."""
    by_bucket = {pb.bucket_id: pb for pb in policy.buckets}
    sent = []
    for line in lines:
        pb = by_bucket.get(line.bucket_id)
        if line.allocated <= 0 or pb is None or pb.frequency is not Frequency.IMMEDIATE:
            continue
        beneficiary = _beneficiary(session, entity_id, pb.destination_account_id)
        if beneficiary is None:
            continue  # stays held until a beneficiary is set
        sent.append(
            _send(session, entity_id, pb.bucket_id, line.allocated, beneficiary, "immediate",
                  transaction_id=txn.id, period=line.period_key)
        )
    target = _beneficiary(session, entity_id, policy.surplus_account_id)
    if surplus > 0 and target is not None and target.kind is not AccountKind.POOL:
        sent.append(_send(session, entity_id, None, surplus, target, "surplus", transaction_id=txn.id))
    return sent


def _periods(pb: PolicyBucket, today: date) -> tuple[str, str]:
    """(current cycle key, key of the cycle that ended before it)."""
    start, _ = period_bounds(pb.frequency, today, pb.settlement_day, pb.due_date)
    current = period_key(pb.frequency, today, pb.settlement_day, pb.due_date)
    return current, period_key(pb.frequency, start - timedelta(days=1), pb.settlement_day, pb.due_date)


def run_due(session: Session, entity_id: int, today: date) -> list[Payout]:
    """Items with a due date: when a cycle ends its balance transfers (excluding the new cycle's share).

    Safe to repeat: a paid cycle is recorded, so it is never paid twice.
    """
    from .policies import active_policy

    policy = active_policy(session, entity_id)
    scheduled = [pb for pb in policy.buckets if pb.frequency is not Frequency.IMMEDIATE] if policy else []
    if not scheduled:
        return []
    _lock_entity(session, entity_id)
    current_balances = balances(session, entity_id)
    sent = []
    for pb in scheduled:
        beneficiary = _beneficiary(session, entity_id, pb.destination_account_id)
        if beneficiary is None:
            continue
        current, previous = _periods(pb, today)
        already = session.scalar(
            select(Payout.id).where(
                Payout.bucket_id == pb.bucket_id, Payout.reason == "due", Payout.period_key == previous,
                Payout.status == PayoutStatus.SENT,
            )
        )
        if already:
            continue
        amount = current_balances.get(pb.bucket_id, 0) - _allocated_in_period(session, pb.bucket_id, current)
        if amount > 0:
            sent.append(_send(session, entity_id, pb.bucket_id, amount, beneficiary, "due", period=previous))
    return sent


def run_due_all(session: Session, today: date) -> int:
    count = 0
    for entity_id in session.scalars(select(Entity.id)):
        count += len(run_due(session, entity_id, today))
    return count


def reversal_blocker(session: Session, txn: Transaction) -> str | None:
    """A reversal restores item balances, so it is not allowed once money went to a beneficiary."""
    if session.scalar(
        select(Payout.id).where(Payout.transaction_id == txn.id, Payout.status == PayoutStatus.SENT)
    ):
        return "حُوِّل جزء من هذه المعاملة إلى المستفيدين، لذا لا يمكن عكسها تلقائيًا. صحّحها بمعاملة تسوية"
    current = balances(session, txn.entity_id)
    if any(current.get(e.bucket_id, 0) < e.allocated for e in txn.events if e.allocated > 0):
        return "حُوِّل رصيد أحد بنود هذه المعاملة إلى المستفيد، لذا لا يمكن عكسها تلقائيًا. صحّحها بمعاملة تسوية"
    return None


# ------------------------------------------------------------------ view


@dataclass
class HeldView:
    bucket_id: int
    name: str
    balance: int
    beneficiary: BankAccount | None
    next_due: date | None  # empty = transfers with each deposit
    target: int  # cycle target for a fixed amount
    funded: int  # funded in the current cycle
    in_policy: bool


def held_balances(session: Session, entity_id: int, today: date) -> list[HeldView]:
    """Items of the active policy, plus any removed item that still has a held balance."""
    from .policies import active_policy

    policy = active_policy(session, entity_id)
    current = balances(session, entity_id)
    states = {
        s.bucket_id: s
        for s in session.scalars(select(BucketPeriodState).where(BucketPeriodState.entity_id == entity_id))
    }
    views: list[HeldView] = []
    seen: set[int] = set()
    for pb in policy.buckets if policy else []:
        seen.add(pb.bucket_id)
        _, end = period_bounds(pb.frequency, today, pb.settlement_day, pb.due_date)
        state = states.get(pb.bucket_id)
        views.append(
            HeldView(
                bucket_id=pb.bucket_id,
                name=pb.name,
                balance=current.get(pb.bucket_id, 0),
                beneficiary=session.get(BankAccount, pb.destination_account_id) if pb.destination_account_id else None,
                next_due=end,
                target=state.target if state else 0,
                funded=state.funded if state else 0,
                in_policy=True,
            )
        )
    for bucket_id, balance in current.items():
        if bucket_id in seen or balance <= 0:
            continue
        last = session.scalar(
            select(PolicyBucket).where(PolicyBucket.bucket_id == bucket_id).order_by(PolicyBucket.id.desc()).limit(1)
        )
        views.append(
            HeldView(bucket_id=bucket_id, name=last.name if last else f"بند {bucket_id}", balance=balance,
                     beneficiary=None, next_due=None, target=0, funded=0, in_policy=False)
        )
    return views


def recent_payouts(session: Session, entity_id: int, limit: int = 100) -> list[Payout]:
    return list(
        session.scalars(
            select(Payout).where(Payout.entity_id == entity_id).order_by(Payout.id.desc()).limit(limit)
        )
    )
