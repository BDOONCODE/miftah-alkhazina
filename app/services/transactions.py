"""تسجيل المعاملات الواردة وتقسيمها فورًا، والقيد العكسي لآخر معاملة."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.allocation import ClosedPeriodError, allocate
from ..domain.periods import local_date
from ..domain.types import BucketState
from ..models import (
    AllocationEvent,
    AuditLog,
    BucketPeriodState,
    Entity,
    PeriodClosure,
    Transaction,
    TransactionSource,
    User,
)
from .policies import active_policy, specs


class TransactionError(ValueError):
    pass


def _lock_entity(session: Session, entity_id: int) -> Entity:
    """يسلسل التخصيص لكل كيان (FOR UPDATE على PostgreSQL؛ SQLite يسلسل الكتابة أصلًا)."""
    return session.scalars(select(Entity).where(Entity.id == entity_id).with_for_update()).one()


def _load_states(session: Session, entity_id: int) -> dict[int, BucketPeriodState]:
    rows = session.scalars(select(BucketPeriodState).where(BucketPeriodState.entity_id == entity_id))
    return {row.bucket_id: row for row in rows}


def _as_domain(row: BucketPeriodState) -> BucketState:
    return BucketState(row.bucket_id, row.period_key, row.target, row.funded, row.carried_deficit)


def record_transaction(
    session: Session,
    entity: Entity,
    user: User,
    amount: int,
    occurred_at: datetime,
    note: str = "",
    source: TransactionSource = TransactionSource.MANUAL,
    external_ref: str | None = None,
) -> Transaction:
    _lock_entity(session, entity.id)

    if external_ref:
        existing = session.scalar(select(Transaction).where(Transaction.external_ref == external_ref))
        if existing is not None:
            return existing  # نفس المعاملة وصلت مرة ثانية

    policy = active_policy(session, entity.id)
    if policy is None:
        raise TransactionError("ما في سياسة نشطة لهذي الشركة. لازم يعتمد المراجع السياسة قبل تقسيم أي مبلغ")

    rows = _load_states(session, entity.id)
    try:
        result = allocate(specs(policy), {k: _as_domain(v) for k, v in rows.items()}, amount, local_date(occurred_at))
    except ClosedPeriodError as exc:
        raise TransactionError(str(exc)) from exc
    except ValueError as exc:
        raise TransactionError(str(exc)) from exc

    txn = Transaction(
        entity_id=entity.id,
        amount=amount,
        surplus=result.surplus,
        occurred_at=occurred_at,
        source=source,
        external_ref=external_ref or None,
        policy_id=policy.id,
        note=note.strip(),
        created_by=user.id,
    )
    session.add(txn)
    session.flush()

    for line in result.lines:
        before = line.state_before
        session.add(
            AllocationEvent(
                transaction_id=txn.id,
                bucket_id=line.bucket_id,
                period_key=line.period_key,
                required=line.required,
                allocated=line.allocated,
                status=line.status,
                funded_after=line.state_after.funded,
                carried_deficit_after=line.state_after.carried_deficit,
                before_period_key=before.period_key if before else None,
                before_target=before.target if before else None,
                before_funded=before.funded if before else None,
                before_carried_deficit=before.carried_deficit if before else None,
            )
        )
        after = line.state_after
        row = rows.get(line.bucket_id)
        if row is None:
            row = BucketPeriodState(bucket_id=line.bucket_id, entity_id=entity.id)
            session.add(row)
        row.period_key, row.target, row.funded, row.carried_deficit = (
            after.period_key,
            after.target,
            after.funded,
            after.carried_deficit,
        )

    for closure in result.closures:
        session.add(
            PeriodClosure(
                entity_id=entity.id,
                bucket_id=closure.bucket_id,
                period_key=closure.period_key,
                target=closure.target,
                funded=closure.funded,
                closed_deficit=closure.closed_deficit,
                closed_by_transaction_id=txn.id,
            )
        )

    session.add(
        AuditLog(
            user_id=user.id,
            entity_id=entity.id,
            action="transaction.recorded",
            details={"transaction_id": txn.id, "amount": amount, "surplus": result.surplus},
        )
    )
    return txn


def is_reversed(session: Session, txn: Transaction) -> bool:
    return session.scalar(select(Transaction.id).where(Transaction.reversal_of == txn.id)) is not None


def reversible_transaction(session: Session, entity_id: int) -> Transaction | None:
    """آخر معاملة أصلية غير معكوسة — هي الوحيدة اللي ينفع نعكسها (الأحدث أولًا)."""
    reversed_ids = set(
        session.scalars(
            select(Transaction.reversal_of).where(Transaction.entity_id == entity_id, Transaction.reversal_of.is_not(None))
        )
    )
    for txn in session.scalars(
        select(Transaction).where(Transaction.entity_id == entity_id).order_by(Transaction.id.desc())
    ):
        if txn.reversal_of is None and txn.id not in reversed_ids:
            return txn
    return None


def reverse_transaction(session: Session, txn: Transaction, user: User, reason: str) -> Transaction:
    _lock_entity(session, txn.entity_id)
    if not reason.strip():
        raise TransactionError("اكتب سبب التصحيح")
    latest = reversible_transaction(session, txn.entity_id)
    if latest is None or latest.id != txn.id:
        raise TransactionError("التصحيح متاح لآخر معاملة فقط. المعاملات الأقدم تتصحح بمعاملة تسوية")
    policy = active_policy(session, txn.entity_id)
    if policy is None or policy.id != txn.policy_id:
        raise TransactionError("تغيّرت السياسة بعد هذي المعاملة، فما يمكن عكسها تلقائيًا")

    reversal = Transaction(
        entity_id=txn.entity_id,
        amount=-txn.amount,
        surplus=-txn.surplus,
        occurred_at=txn.occurred_at,  # نفس الفترة، فالتقارير تصفّي الأثر
        source=txn.source,
        reversal_of=txn.id,
        policy_id=txn.policy_id,
        note=reason.strip(),
        created_by=user.id,
    )
    session.add(reversal)
    session.flush()

    rows = _load_states(session, txn.entity_id)
    for event in txn.events:
        row = rows.get(event.bucket_id)
        if event.before_period_key is None:
            if row is not None:
                session.delete(row)
            restored = (None, 0, 0, 0)
        else:
            if row is None:
                row = BucketPeriodState(bucket_id=event.bucket_id, entity_id=txn.entity_id)
                session.add(row)
            restored = (event.before_period_key, event.before_target, event.before_funded, event.before_carried_deficit)
            row.period_key, row.target, row.funded, row.carried_deficit = restored
        session.add(
            AllocationEvent(
                transaction_id=reversal.id,
                bucket_id=event.bucket_id,
                period_key=event.period_key,
                required=-event.required,
                allocated=-event.allocated,
                status=event.status,
                funded_after=restored[2],
                carried_deficit_after=restored[3],
            )
        )

    for closure in session.scalars(select(PeriodClosure).where(PeriodClosure.closed_by_transaction_id == txn.id)):
        closure.voided = True

    session.add(
        AuditLog(
            user_id=user.id,
            entity_id=txn.entity_id,
            action="transaction.reversed",
            details={"transaction_id": txn.id, "reversal_id": reversal.id, "reason": reason.strip()},
        )
    )
    return reversal
