"""Support and integration (the general admin): follows the bank link per company, failed transfers and
reversal requests, without seeing company data, IBANs or amounts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    AuditLog,
    Entity,
    Payout,
    PayoutStatus,
    ReversalRequest,
    Transaction,
    TransactionSource,
    TransferIssue,
    User,
    utcnow,
)

LEG_LABELS = {
    "sweep": "حساب الشركة ← الحساب المجمّع",
    "move": "الحساب المجمّع ← حساب البند الافتراضي",
    "payout": "حساب البند الافتراضي ← المستفيد",
    "unwind": "عكس معاملة ← حساب الشركة",
}
REQUEST_STATUS = {"open": "جديد", "in_progress": "قيد المعالجة", "done": "تم", "rejected": "مرفوض"}
OPEN_STATUSES = ("open", "in_progress")


class SupportError(ValueError):
    pass


@dataclass
class CompanyLink:
    entity: Entity
    last_bank_notice: datetime | None
    transactions: int
    transfers_sent: int
    open_issues: int


def integration_status(session: Session) -> list[CompanyLink]:
    """Per company: when the bank last notified us, and how many operations went through (counts, no amounts)."""
    def per_entity(stmt):
        return dict(session.execute(stmt).all())

    last_notice = per_entity(
        select(Transaction.entity_id, func.max(Transaction.created_at))
        .where(Transaction.source == TransactionSource.OPEN_BANKING).group_by(Transaction.entity_id)
    )
    txns = per_entity(select(Transaction.entity_id, func.count()).group_by(Transaction.entity_id))
    sent = per_entity(
        select(Payout.entity_id, func.count()).where(Payout.status == PayoutStatus.SENT).group_by(Payout.entity_id)
    )
    issues = per_entity(
        select(TransferIssue.entity_id, func.count()).where(TransferIssue.resolved.is_(False))
        .group_by(TransferIssue.entity_id)
    )
    return [
        CompanyLink(e, last_notice.get(e.id), txns.get(e.id, 0), sent.get(e.id, 0), issues.get(e.id, 0))
        for e in session.scalars(select(Entity).order_by(Entity.name))
    ]


def _names(session: Session) -> dict[int, str]:
    return dict(session.execute(select(Entity.id, Entity.name)).all())


def issues(session: Session, limit: int = 50) -> list[TransferIssue]:
    """Open issues first, then the most recent handled ones."""
    rows = list(session.scalars(
        select(TransferIssue).order_by(TransferIssue.resolved, TransferIssue.created_at.desc()).limit(limit)
    ))
    names = _names(session)
    for row in rows:
        row.entity_name = names.get(row.entity_id, "")
    return rows


def resolve_issue(session: Session, issue: TransferIssue, user: User, note: str) -> None:
    issue.resolved, issue.note, issue.resolved_by = True, note.strip(), user.id
    session.add(AuditLog(user_id=user.id, entity_id=issue.entity_id, action="support.issue_resolved",
                         details={"issue_id": issue.id, "reference": issue.reference}))


def requests(session: Session, limit: int = 50) -> list[ReversalRequest]:
    """Open requests first (oldest first, so nothing waits forever), then the most recent closed ones."""
    open_rows = session.scalars(
        select(ReversalRequest).where(ReversalRequest.status.in_(OPEN_STATUSES)).order_by(ReversalRequest.created_at)
    ).all()
    closed = session.scalars(
        select(ReversalRequest).where(ReversalRequest.status.not_in(OPEN_STATUSES))
        .order_by(ReversalRequest.updated_at.desc()).limit(limit)
    ).all()
    rows = [*open_rows, *closed]
    names = _names(session)
    refs = dict(session.execute(
        select(Transaction.id, Transaction.external_ref).where(Transaction.id.in_([r.transaction_id for r in rows]))
    ).all())
    for row in rows:
        row.entity_name, row.external_ref = names.get(row.entity_id, ""), refs.get(row.transaction_id)
    return rows


def request_reversal(session: Session, txn: Transaction, user: User, reason: str) -> ReversalRequest:
    if not reason.strip():
        raise SupportError("اكتب سبب طلب العكس")
    if txn.reversal_of is not None:
        raise SupportError("هذا قيد عكسي ولا يُعكس")
    if session.scalar(select(Transaction.id).where(Transaction.reversal_of == txn.id)):
        raise SupportError("عُكست هذه المعاملة من قبل")
    if open_request_for(session, txn.id):
        raise SupportError("يوجد طلب عكس مفتوح لهذه المعاملة")
    row = ReversalRequest(entity_id=txn.entity_id, transaction_id=txn.id, reason=reason.strip(), requested_by=user.id)
    session.add(row)
    session.flush()
    session.add(AuditLog(user_id=user.id, entity_id=txn.entity_id, action="support.reversal_requested",
                         details={"request_id": row.id, "transaction_id": txn.id}))
    return row


def open_request_for(session: Session, transaction_id: int) -> ReversalRequest | None:
    return session.scalar(select(ReversalRequest).where(
        ReversalRequest.transaction_id == transaction_id, ReversalRequest.status.in_(OPEN_STATUSES)
    ))


def requests_for(session: Session, transaction_id: int) -> list[ReversalRequest]:
    return list(session.scalars(
        select(ReversalRequest).where(ReversalRequest.transaction_id == transaction_id).order_by(ReversalRequest.id)
    ))


def update_request(session: Session, row: ReversalRequest, user: User, status: str, note: str) -> None:
    if status not in REQUEST_STATUS:
        raise SupportError("حالة غير معروفة")
    row.status, row.note, row.handled_by, row.updated_at = status, note.strip(), user.id, utcnow()
    session.add(AuditLog(user_id=user.id, entity_id=row.entity_id, action="support.reversal_updated",
                         details={"request_id": row.id, "status": status}))
