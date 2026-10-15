"""Recording incoming transactions and splitting them immediately, and reversing the latest one."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.allocation import ClosedPeriodError, allocate
from ..domain.periods import local_date
from ..domain.types import BucketState
from ..models import (
    AccountKind,
    AllocationEvent,
    AuditLog,
    BankAccount,
    BucketPeriodState,
    Entity,
    HeldDeposit,
    PeriodClosure,
    Transaction,
    TransactionSource,
    User,
    utcnow,
)
from . import payouts
from .accounts import INCOME_KINDS, normalize_iban
from .policies import active_policy, specs


class TransactionError(ValueError):
    pass


def _lock_entity(session: Session, entity_id: int) -> Entity:
    """Serialize allocation per entity (FOR UPDATE on PostgreSQL; SQLite already serializes writes)."""
    return session.scalars(select(Entity).where(Entity.id == entity_id).with_for_update()).one()


def _load_states(session: Session, entity_id: int) -> dict[int, BucketPeriodState]:
    rows = session.scalars(select(BucketPeriodState).where(BucketPeriodState.entity_id == entity_id))
    return {row.bucket_id: row for row in rows}


def _as_domain(row: BucketPeriodState) -> BucketState:
    return BucketState(row.bucket_id, row.period_key, row.target, row.funded, row.carried_deficit)


def record_transaction(
    session: Session,
    entity: Entity,
    user: User | None,
    amount: int,
    occurred_at: datetime,
    note: str = "",
    source: TransactionSource = TransactionSource.MANUAL,
    external_ref: str | None = None,
    source_account_id: int | None = None,
) -> Transaction:
    """user=None: the money arrived automatically from the bank."""
    _lock_entity(session, entity.id)

    if source_account_id is not None:
        account = session.get(BankAccount, source_account_id)
        if account is None or account.entity_id != entity.id or account.kind not in (AccountKind.SOURCE, AccountKind.POOL):
            raise TransactionError("يجب أن يكون حساب المصدر حساب دخل لهذه الشركة")

    if external_ref:
        existing = session.scalar(select(Transaction).where(Transaction.external_ref == external_ref))
        if existing is not None:
            return existing  # the same transaction arrived again

    policy = active_policy(session, entity.id)
    if policy is None:
        raise TransactionError("لا توجد سياسة نشطة لهذه الشركة. يجب اعتماد السياسة قبل تقسيم أي مبلغ")

    # If any items are due for payout, transfer them before splitting the new money
    payouts.run_due(session, entity.id, local_date(utcnow()))

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
        created_by=user.id if user else None,
        source_account_id=source_account_id,
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

    session.flush()
    session.refresh(txn, ["events"])
    # The money is already in the master account: the split is kept in the ledger. The pool's surplus covers
    # items that are behind (nearest due first), before anything transfers out
    covered = payouts.cover_deficits(session, entity.id, policy, local_date(utcnow()), transaction_id=txn.id)
    payouts.pay_on_allocation(session, entity.id, policy, txn, result.lines, covered)

    session.add(
        AuditLog(
            user_id=user.id if user else None,
            entity_id=entity.id,
            action="transaction.recorded",
            details={
                "transaction_id": txn.id,
                "amount": amount,
                "surplus": result.surplus,
                "source": source.value,
            },
        )
    )
    return txn


def is_reversed(session: Session, txn: Transaction) -> bool:
    return session.scalar(select(Transaction.id).where(Transaction.reversal_of == txn.id)) is not None


def reversible_transaction(session: Session, entity_id: int) -> Transaction | None:
    """Latest original, non-reversed transaction: the only one that can be reversed (newest first)."""
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
        raise TransactionError("التصحيح متاح لآخر معاملة فقط، وتُصحَّح المعاملات الأقدم بمعاملة تسوية")
    policy = active_policy(session, txn.entity_id)
    if policy is None or policy.id != txn.policy_id:
        raise TransactionError("تغيّرت السياسة بعد هذه المعاملة، لذا لا يمكن عكسها تلقائيًا")
    if blocker := payouts.reversal_blocker(session, txn):
        raise TransactionError(blocker)

    reversal = Transaction(
        entity_id=txn.entity_id,
        amount=-txn.amount,
        surplus=-txn.surplus,
        occurred_at=txn.occurred_at,  # same period, so reports net out the effect
        source=txn.source,
        reversal_of=txn.id,
        source_account_id=txn.source_account_id,
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

    # Money that really arrived from the bank goes back to the account it came from
    payouts.refund(session, txn, reversal)

    session.add(
        AuditLog(
            user_id=user.id,
            entity_id=txn.entity_id,
            action="transaction.reversed",
            details={"transaction_id": txn.id, "reversal_id": reversal.id, "reason": reason.strip()},
        )
    )
    return reversal


# ------------------------------------------------------------------ incoming from the bank


def ingest_bank_credit(
    session: Session,
    deposit_iban: str,
    sender_iban: str,
    amount: int,
    occurred_at: datetime,
    reference: str,
    sender_name: str = "",
    note: str = "",
) -> Transaction | HeldDeposit:
    """Money reached a company's deposit IBAN (provider notification or the simulator).

    From one of the company's registered accounts: split immediately. From any other account: held until the
    accountant confirms it is theirs. The same reference is never handled twice (banks resend notifications).
    """
    from . import virtual_ibans

    reference = reference.strip()
    if not reference:
        raise TransactionError("الإشعار بدون رقم مرجعي")
    if existing := session.scalar(select(Transaction).where(Transaction.external_ref == reference)):
        return existing
    if held := session.scalar(select(HeldDeposit).where(HeldDeposit.reference == reference)):
        return held
    entity = virtual_ibans.entity_for(session, deposit_iban)
    if entity is None:
        raise TransactionError("آيبان الإيداع غير مخصّص لأي شركة")
    sender_iban = normalize_iban(sender_iban or "")
    account = session.scalar(select(BankAccount).where(
        BankAccount.entity_id == entity.id, BankAccount.iban == sender_iban, BankAccount.is_active,
        BankAccount.kind.in_(INCOME_KINDS),
    )) if sender_iban else None
    if account is None:
        held = HeldDeposit(entity_id=entity.id, sender_iban=sender_iban, sender_name=sender_name.strip()[:128],
                           amount=amount, occurred_at=occurred_at, reference=reference)
        session.add(held)
        session.flush()
        session.add(AuditLog(entity_id=entity.id, action="deposit.held",
                             details={"held_id": held.id, "amount": amount, "from": sender_iban[-4:]}))
        return held
    return record_transaction(
        session, entity, None, amount, occurred_at, note=note, source=TransactionSource.OPEN_BANKING,
        external_ref=reference, source_account_id=account.id,
    )


def held_deposits(session: Session, entity_id: int) -> list[HeldDeposit]:
    return list(session.scalars(
        select(HeldDeposit).where(HeldDeposit.entity_id == entity_id, HeldDeposit.status == "pending")
        .order_by(HeldDeposit.id)
    ))


def accept_held(session: Session, held: HeldDeposit, user: User, register: bool) -> Transaction:
    """The accountant confirms the money is theirs: it is split now, and the sender account can be registered
    so its next transfers are split directly."""
    from .accounts import AccountError, add_account

    if held.status != "pending":
        raise TransactionError("تم البت في هذه الحوالة من قبل")
    entity = session.get(Entity, held.entity_id)
    source_id = None
    if register and held.sender_iban:
        existing = session.scalar(select(BankAccount).where(
            BankAccount.entity_id == entity.id, BankAccount.iban == held.sender_iban
        ))
        if existing is None:
            try:
                existing = add_account(session, entity, user, name=held.sender_name or f"حساب ••{held.sender_iban[-4:]}",
                                       bank_name="", iban=held.sender_iban, kind="source")
            except AccountError as exc:
                raise TransactionError(str(exc)) from exc
        source_id = existing.id if existing.kind in INCOME_KINDS else None
    txn = record_transaction(session, entity, user, held.amount, held.occurred_at, note="حوالة مؤكَّدة من المحاسب",
                             source=TransactionSource.OPEN_BANKING, external_ref=held.reference,
                             source_account_id=source_id)
    held.status, held.transaction_id, held.decided_by, held.decided_at = "accepted", txn.id, user.id, utcnow()
    return txn


def reject_held(session: Session, held: HeldDeposit, user: User) -> None:
    """Not the company's money: support returns it to the sender."""
    from ..models import TransferIssue

    if held.status != "pending":
        raise TransactionError("تم البت في هذه الحوالة من قبل")
    held.status, held.decided_by, held.decided_at = "rejected", user.id, utcnow()
    session.add(TransferIssue(entity_id=held.entity_id, leg="deposit", destination=held.sender_name or held.sender_iban,
                              reference=held.reference, error="رفضت الشركة الحوالة: تحتاج إرجاعًا إلى المرسل"))
    session.add(AuditLog(user_id=user.id, entity_id=held.entity_id, action="deposit.rejected",
                         details={"held_id": held.id}))
