"""Transferring item balances to beneficiaries according to their schedule.

Flow: income lands in the pool account -> is split -> each item's share is held in its own virtual account
(at the banking provider, in the background; users never deal with it) -> and is transferred to the beneficiary:
  - an item with no due date (immediate): transfers as soon as it is split.
  - an item with a due date: accumulates and transfers in full on the due date (first day after the cycle ends).
The surplus stays in the pool during the month. If an item falls short of its schedule, the pool covers it
first (by priority). At the start of each month the previous month's remaining surplus is isolated in the
"surplus" small account, and transfers from there to the account the accountant chose (if any).
Nothing ever leaves for a real account except from a small account.

Held balance of an item = allocated + covered by the pool - successful transfers from it.
Pool balance = all surplus - covers - isolations (- legacy surplus transfers that left the pool directly).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..domain.periods import RIYADH, accrued_target, period_bounds, period_key
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
    PoolMove,
    Transaction,
)
from .accounts import DESTINATION_KINDS
from .banking import BankingError, provider

REASON_LABELS = {"immediate": "فور التقسيم", "due": "يوم الاستحقاق", "surplus": "الفائض الشهري"}
SURPLUS_NAME = "الفائض"


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


def _sum(session: Session, stmt) -> int:
    return int(session.scalar(stmt) or 0)


def pool_balance(session: Session, entity_id: int) -> int:
    """The current surplus waiting in the pool: all surplus - covers - isolations - legacy direct transfers."""
    session.flush()
    surplus = _sum(session, select(func.coalesce(func.sum(Transaction.surplus), 0)).where(Transaction.entity_id == entity_id))
    moved = _sum(session, select(func.coalesce(func.sum(PoolMove.amount), 0)).where(PoolMove.entity_id == entity_id))
    legacy = _sum(session, select(func.coalesce(func.sum(Payout.amount), 0)).where(
        Payout.entity_id == entity_id, Payout.bucket_id.is_(None), Payout.status == PayoutStatus.SENT
    ))
    return surplus - moved - legacy


def surplus_bucket(session: Session, entity_id: int) -> Bucket:
    """The "surplus" small account (opened once, with its virtual account at the provider)."""
    entity = session.get(Entity, entity_id)
    if entity.surplus_bucket_id is None:
        bucket = Bucket(entity_id=entity_id, system_name=SURPLUS_NAME)
        session.add(bucket)
        session.flush()
        entity.surplus_bucket_id = bucket.id
    bucket = session.get(Bucket, entity.surplus_bucket_id)
    if bucket.virtual_ref is None:
        bucket.virtual_iban, bucket.virtual_ref = provider().open_virtual_account(entity_id, bucket.id, SURPLUS_NAME)
    return bucket


def isolated_surplus(session: Session, entity_id: int) -> int:
    """Balance of the "surplus" small account (isolated monthly surplus not yet transferred out)."""
    entity = session.get(Entity, entity_id)
    if entity.surplus_bucket_id is None:
        return 0
    return balances(session, entity_id).get(entity.surplus_bucket_id, 0)


def _move_from_pool(
    session: Session, entity_id: int, kind: str, bucket: Bucket, amount: int, period: str, transaction_id: int | None
) -> PoolMove:
    if bucket.virtual_ref is None:
        ensure_virtual_accounts(session, entity_id, [bucket.id])
    move = PoolMove(entity_id=entity_id, kind=kind, bucket_id=bucket.id, amount=amount, period_key=period,
                    transaction_id=transaction_id)
    session.add(move)
    session.flush()
    try:
        move.move_ref = provider().move(
            from_ref=ensure_pool(session, entity_id), to_ref=bucket.virtual_ref, amount=amount, reference=f"PM-{move.id}"
        )
    except BankingError as exc:
        raise PayoutError(f"تعذّر تنفيذ العملية لدى البنك: {exc}") from exc
    session.add(AuditLog(entity_id=entity_id, action=f"pool.{kind}",
                         details={"bucket_id": bucket.id, "amount": amount, "period": period}))
    return move


# ------------------------------------------------------------------ covering deficits from the pool


def _deficit(pb, state: BucketPeriodState, today: date, *, period_ended: bool) -> int:
    """How far an item is behind. Rates: their carried deficit. Fixed: behind schedule as of today,
    or the whole remaining target once the period has ended."""
    if pb.calc_type.is_rate:
        return state.carried_deficit
    if period_ended:
        return max(0, state.target - state.funded)
    due = accrued_target(state.target, pb.frequency, today, pb.settlement_day, pb.due_date)
    return max(0, due - state.funded)


def _due_on(pb, today: date) -> date:
    """When the item's current cycle is due (undated items are due today)."""
    _, end = period_bounds(pb.frequency, today, pb.settlement_day, pb.due_date)
    return end or today


def _cover(session: Session, entity_id: int, pb, state: BucketPeriodState, amount: int, transaction_id: int | None) -> PoolMove:
    move = _move_from_pool(session, entity_id, "cover", session.get(Bucket, pb.bucket_id), amount,
                           state.period_key, transaction_id)
    state.funded += amount
    if pb.calc_type.is_rate:
        state.carried_deficit -= amount
    return move


def cover_deficits(session: Session, entity_id: int, policy: Policy, today: date,
                   transaction_id: int | None = None) -> dict[int, int]:
    """The pool's surplus covers items that are behind in their current period.

    Order: the nearest due date first (undated items are due now, so they come first), ties by priority.
    Returns {bucket_id: amount covered}."""
    available = pool_balance(session, entity_id)
    covered: dict[int, int] = {}
    if available <= 0:
        return covered
    states = {s.bucket_id: s for s in session.scalars(
        select(BucketPeriodState).where(BucketPeriodState.entity_id == entity_id))}
    for pb in sorted(policy.buckets, key=lambda b: (_due_on(b, today), b.priority)):
        state = states.get(pb.bucket_id)
        if state is None or state.period_key != period_key(pb.frequency, today, pb.settlement_day, pb.due_date):
            continue
        amount = min(_deficit(pb, state, today, period_ended=False), available)
        if amount > 0:
            _cover(session, entity_id, pb, state, amount, transaction_id)
            covered[pb.bucket_id] = amount
            available -= amount
        if available <= 0:
            break
    return covered


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
    received = session.execute(
        select(PoolMove.bucket_id, func.sum(PoolMove.amount)).where(PoolMove.entity_id == entity_id)
        .group_by(PoolMove.bucket_id)
    )
    for bucket_id, total in received:
        result[bucket_id] = result.get(bucket_id, 0) + int(total or 0)
    return result


def _allocated_in_period(session: Session, bucket_id: int, key: str) -> int:
    """What an item received for one period: its split shares plus any cover from the pool."""
    split = _sum(session, select(func.coalesce(func.sum(AllocationEvent.allocated), 0)).where(
        AllocationEvent.bucket_id == bucket_id, AllocationEvent.period_key == key
    ))
    covered = _sum(session, select(func.coalesce(func.sum(PoolMove.amount), 0)).where(
        PoolMove.bucket_id == bucket_id, PoolMove.kind == "cover", PoolMove.period_key == key
    ))
    return split + covered


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
    session: Session, entity_id: int, policy: Policy, txn: Transaction, lines: tuple[AllocationLine, ...],
    covered: dict[int, int] | None = None,
) -> list[Payout]:
    """Right after the split: items with no due date transfer their share (plus any cover from the pool).
    The surplus stays in the pool until the monthly isolation."""
    covered = covered or {}
    by_bucket = {pb.bucket_id: pb for pb in policy.buckets}
    sent = []
    for line in lines:
        pb = by_bucket.get(line.bucket_id)
        amount = line.allocated + covered.get(line.bucket_id, 0)
        if amount <= 0 or pb is None or pb.frequency is not Frequency.IMMEDIATE:
            continue
        beneficiary = _beneficiary(session, entity_id, pb.destination_account_id)
        if beneficiary is None:
            continue  # stays held until a beneficiary is set
        sent.append(
            _send(session, entity_id, pb.bucket_id, amount, beneficiary, "immediate",
                  transaction_id=txn.id, period=line.period_key)
        )
    return sent


def _periods(pb: PolicyBucket, today: date) -> tuple[str, str]:
    """(current cycle key, key of the cycle that ended before it)."""
    start, _ = period_bounds(pb.frequency, today, pb.settlement_day, pb.due_date)
    current = period_key(pb.frequency, today, pb.settlement_day, pb.due_date)
    return current, period_key(pb.frequency, start - timedelta(days=1), pb.settlement_day, pb.due_date)


def run_due(session: Session, entity_id: int, today: date) -> list:
    """Daily housekeeping (also runs before every incoming amount). Safe to repeat.

    1. Items whose cycle just ended: the pool covers any shortfall, then the full balance transfers
       (excluding the new cycle's share). A paid cycle is recorded, so it is never paid twice.
    2. On the surplus schedule (monthly by default): the ended cycle's remaining surplus is isolated.
    Returns the actions taken (payouts and pool moves).
    """
    from .policies import active_policy

    policy = active_policy(session, entity_id)
    if policy is None:
        return []
    _lock_entity(session, entity_id)
    actions: list = []
    due_now = []
    for pb in policy.buckets:
        if pb.frequency is Frequency.IMMEDIATE:
            continue
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
        if not already:
            due_now.append((pb, beneficiary, current, previous))

    # Cover the ended cycles' shortfalls first, so nothing transfers short while the pool has surplus
    available = pool_balance(session, entity_id)
    states = {s.bucket_id: s for s in session.scalars(
        select(BucketPeriodState).where(BucketPeriodState.entity_id == entity_id))}
    for pb, _, _, previous in sorted(due_now, key=lambda d: d[0].priority):
        state = states.get(pb.bucket_id)
        if available <= 0 or state is None or state.period_key != previous:
            continue
        amount = min(_deficit(pb, state, today, period_ended=True), available)
        if amount > 0:
            actions.append(_cover(session, entity_id, pb, state, amount, None))
            available -= amount

    current_balances = balances(session, entity_id)
    for pb, beneficiary, current, previous in due_now:
        amount = current_balances.get(pb.bucket_id, 0) - _allocated_in_period(session, pb.bucket_id, current)
        if amount > 0:
            actions.append(_send(session, entity_id, pb.bucket_id, amount, beneficiary, "due", period=previous))

    actions += isolate_surplus(session, entity_id, policy, today)
    return actions


def isolate_surplus(session: Session, entity_id: int, policy: Policy, today: date) -> list:
    """On the surplus schedule (monthly by default): move the ended cycle's remaining surplus from the pool
    into the surplus account (once per cycle), then transfer it on to the account the accountant chose, if any."""
    frequency = policy.surplus_frequency or Frequency.MONTHLY
    day, due = policy.surplus_settlement_day, policy.surplus_due_date
    try:
        start, _ = period_bounds(frequency, today, day, due)
    except ValueError:
        return []  # incomplete schedule (e.g. no day set): nothing to isolate yet
    if start is None:
        return []
    previous = period_key(frequency, start - timedelta(days=1), day, due)
    if session.scalar(select(PoolMove.id).where(
        PoolMove.entity_id == entity_id, PoolMove.kind == "isolate", PoolMove.period_key == previous
    )):
        return []
    this_cycle = _sum(session, select(func.coalesce(func.sum(Transaction.surplus), 0)).where(
        Transaction.entity_id == entity_id, Transaction.occurred_at >= datetime.combine(start, time(), RIYADH)
    ))
    amount = pool_balance(session, entity_id) - max(0, this_cycle)
    if amount <= 0:
        return []
    bucket = surplus_bucket(session, entity_id)
    actions: list = [_move_from_pool(session, entity_id, "isolate", bucket, amount, previous, None)]
    beneficiary = _beneficiary(session, entity_id, policy.surplus_account_id)
    if beneficiary is not None:
        actions.append(_send(session, entity_id, bucket.id, amount, beneficiary, "surplus", period=previous))
    return actions


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
    if session.scalar(select(PoolMove.id).where(
        PoolMove.entity_id == txn.entity_id, PoolMove.created_at >= txn.created_at
    )):
        return "استُخدم الفائض بعد هذه المعاملة لتغطية عجز أو عُزل، لذا لا يمكن عكسها تلقائيًا. صحّحها بمعاملة تسوية"
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
    surplus_id = session.get(Entity, entity_id).surplus_bucket_id
    for bucket_id, balance in current.items():
        if bucket_id in seen or bucket_id == surplus_id or balance <= 0:
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
