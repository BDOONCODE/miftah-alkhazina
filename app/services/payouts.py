"""Transferring item balances to beneficiaries according to their schedule.

Flow: the client transfers its revenue to its virtual IBAN -> the money lands in Miftah's master account ->
it is split in our ledger -> each item's share is held (a ledger balance, not a bank account) -> and paid out
from the master to the item's beneficiary, in Miftah's name with a description naming the client:
  - an item with no due date (immediate): transfers as soon as it is split.
  - an item with a due date: accumulates and transfers in full on the due date (first day after the cycle ends).
The surplus stays in the company's balance in the master account. If an item falls short of its schedule, the
surplus covers it first (nearest due date first). On the surplus schedule the ended cycle's remaining surplus is
paid out to the account the accountant chose; without one it simply stays in the company's balance. The company
can also transfer its surplus to one of its own accounts at any time.

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
    TransferIssue,
)
from .accounts import DESTINATION_KINDS
from .banking import BankingError, provider

REASON_LABELS = {"immediate": "فور التقسيم", "due": "يوم الاستحقاق", "surplus": "الفائض",
                 "withdrawal": "سحب إلى حساب الشركة"}
SURPLUS_NAME = "الفائض"


class PayoutError(RuntimeError):
    """A money movement at the banking provider failed. `issue` describes the failed leg for the support page;
    the caller rolls the work back, then calls record_failure so the issue survives the rollback."""

    def __init__(self, message: str, issue: dict | None = None):
        super().__init__(message)
        self.issue = issue


def record_failure(session: Session, exc: BaseException) -> None:
    """After a rollback: keep the failed leg (if any) on the support page."""
    while exc is not None:
        issue = getattr(exc, "issue", None)
        if issue:
            session.add(TransferIssue(**issue))
            session.commit()
            return
        exc = exc.__cause__


def _bucket_name(session: Session, bucket: Bucket) -> str:
    if bucket.system_name:
        return bucket.system_name
    name = session.scalar(
        select(PolicyBucket.name).where(PolicyBucket.bucket_id == bucket.id).order_by(PolicyBucket.id.desc()).limit(1)
    )
    return name or f"بند #{bucket.id}"


def _lock_entity(session: Session, entity_id: int) -> None:
    session.scalars(select(Entity).where(Entity.id == entity_id).with_for_update()).one()


# ------------------------------------------------------------------ the master account


def master_account() -> str:
    """Every client's money is held in Miftah's single master account at the provider."""
    return provider().master_ref()


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
    """The company's "surplus" balance, where the set-aside surplus is held (created once)."""
    entity = session.get(Entity, entity_id)
    if entity.surplus_bucket_id is None:
        bucket = Bucket(entity_id=entity_id, system_name=SURPLUS_NAME)
        session.add(bucket)
        session.flush()
        entity.surplus_bucket_id = bucket.id
    return session.get(Bucket, entity.surplus_bucket_id)


def isolated_surplus(session: Session, entity_id: int) -> int:
    """Surplus set aside under the earlier model (a separate surplus balance); kept so old data still counts."""
    entity = session.get(Entity, entity_id)
    if entity.surplus_bucket_id is None:
        return 0
    return balances(session, entity_id).get(entity.surplus_bucket_id, 0)


def surplus_balance(session: Session, entity_id: int) -> int:
    """The company's surplus: what is left in its balance after the items."""
    return pool_balance(session, entity_id) + isolated_surplus(session, entity_id)


def _move_from_pool(
    session: Session, entity_id: int, kind: str, bucket: Bucket, amount: int, period: str, transaction_id: int | None
) -> PoolMove:
    """A ledger move inside the master account: the pool's surplus to an item (cover) or set aside (isolate)."""
    move = PoolMove(entity_id=entity_id, kind=kind, bucket_id=bucket.id, amount=amount, period_key=period,
                    transaction_id=transaction_id)
    session.add(move)
    session.flush()
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
            from_ref=master_account(), to_iban=beneficiary.iban, amount=amount, reference=payout.reference,
            description=_description(session, entity_id, bucket_id, reason, period),
        )
        payout.status = PayoutStatus.SENT
    except BankingError as exc:
        payout.status, payout.error = PayoutStatus.FAILED, str(exc)
        session.add(TransferIssue(entity_id=entity_id, leg="payout", destination=beneficiary.name,
                                  reference=payout.reference, error=str(exc)))
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


def _description(session: Session, entity_id: int, bucket_id: int | None, reason: str, period: str | None) -> str:
    """Payouts leave in Miftah's name, so the description tells the beneficiary who the money is from and why."""
    company = session.get(Entity, entity_id).name
    if reason == "withdrawal":
        purpose = "سحب من الرصيد"
    else:
        purpose = _bucket_name(session, session.get(Bucket, bucket_id)) if bucket_id else SURPLUS_NAME
    return f"نيابة عن {company}: {purpose}" + (f" {period}" if period else "")


def withdraw(session: Session, entity: Entity, user, amount: int, account_id: int) -> list[Payout]:
    """Transfer surplus now, to one of the company's own registered accounts only."""
    from .accounts import INCOME_KINDS

    account = session.get(BankAccount, account_id) if account_id else None
    if account is None or account.entity_id != entity.id or not account.is_active or account.kind not in INCOME_KINDS:
        raise PayoutError("السحب متاح إلى حسابات الشركة المسجّلة فقط")
    if amount <= 0:
        raise PayoutError("يجب أن يكون مبلغ السحب أكبر من صفر")
    _lock_entity(session, entity.id)
    if amount > surplus_balance(session, entity.id):
        raise PayoutError("المبلغ أكبر من رصيد الفائض")
    sent = []
    legacy = min(amount, isolated_surplus(session, entity.id))  # surplus set aside under the earlier model first
    if legacy > 0:
        sent.append(_send(session, entity.id, surplus_bucket(session, entity.id).id, legacy, account, "withdrawal"))
    if amount - legacy > 0:
        sent.append(_send(session, entity.id, None, amount - legacy, account, "withdrawal"))
    session.add(AuditLog(user_id=user.id, entity_id=entity.id, action="surplus.withdrawn",
                         details={"amount": amount, "to": account.name, "payouts": [p.id for p in sent]}))
    return sent


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
    2. On the surplus schedule (monthly by default): the ended cycle's remaining surplus is paid out.
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

    actions += pay_surplus(session, entity_id, policy, today)
    return actions


def pay_surplus(session: Session, entity_id: int, policy: Policy, today: date) -> list:
    """On the surplus schedule (monthly by default): pay the ended cycle's remaining surplus to the account the
    accountant chose, once per cycle. Without a chosen account it stays in the company's balance."""
    beneficiary = _beneficiary(session, entity_id, policy.surplus_account_id)
    if beneficiary is None:
        return []
    frequency = policy.surplus_frequency or Frequency.MONTHLY
    day, due = policy.surplus_settlement_day, policy.surplus_due_date
    try:
        start, _ = period_bounds(frequency, today, day, due)
    except ValueError:
        return []  # incomplete schedule (e.g. no day set): nothing to pay yet
    if start is None:
        return []
    previous = period_key(frequency, start - timedelta(days=1), day, due)
    if session.scalar(select(Payout.id).where(
        Payout.entity_id == entity_id, Payout.reason == "surplus", Payout.period_key == previous,
        Payout.status == PayoutStatus.SENT,
    )):
        return []
    this_cycle = _sum(session, select(func.coalesce(func.sum(Transaction.surplus), 0)).where(
        Transaction.entity_id == entity_id, Transaction.occurred_at >= datetime.combine(start, time(), RIYADH)
    ))
    amount = pool_balance(session, entity_id) - max(0, this_cycle)
    if amount <= 0:
        return []
    return [_send(session, entity_id, None, amount, beneficiary, "surplus", period=previous)]


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
