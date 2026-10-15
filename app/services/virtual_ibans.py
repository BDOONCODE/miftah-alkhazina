"""Deposit IBANs: the virtual IBANs Neotek issues under Miftah's master account, one per client company.

The admin loads the IBANs Neotek gives us into an inventory. A company gets one automatically when its first
policy is approved (money that arrives before a policy exists could not be split). A released IBAN is frozen
for FREEZE_DAYS before it can go to another company: the old company may still have a standing order running.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import AuditLog, Entity, Policy, PolicyStatus, User, VirtualIban, utcnow
from .accounts import IBAN_RE, normalize_iban
from .banking import is_simulated

FREEZE_DAYS = 90
LOW_STOCK = 10


class VirtualIbanError(ValueError):
    pass


def add_ibans(session: Session, text: str, user: User) -> tuple[int, list[str]]:
    """Load IBANs (one per line, or separated by commas). Returns (how many were added, rejected lines)."""
    added, rejected = 0, []
    known = set(session.scalars(select(VirtualIban.iban)))
    for raw in text.replace(",", "\n").splitlines():
        if not raw.strip():
            continue
        iban = normalize_iban(raw)
        if not IBAN_RE.match(iban) or iban in known:
            rejected.append(raw.strip())
            continue
        session.add(VirtualIban(iban=iban))
        known.add(iban)
        added += 1
    session.flush()
    if added:
        session.add(AuditLog(user_id=user.id, action="virtual_iban.added", details={"count": added}))
        assign_waiting(session)
    return added, rejected


def _thaw(session: Session) -> None:
    """Frozen IBANs whose freeze is over go back to the inventory."""
    limit = utcnow() - timedelta(days=FREEZE_DAYS)
    for row in session.scalars(select(VirtualIban).where(VirtualIban.status == "frozen", VirtualIban.released_at <= limit)):
        row.status = "available"


def for_entity(session: Session, entity_id: int) -> VirtualIban | None:
    return session.scalar(select(VirtualIban).where(VirtualIban.entity_id == entity_id, VirtualIban.status == "assigned"))


def current(session: Session, entity: Entity) -> VirtualIban | None:
    """The company's deposit IBAN, assigned now if its policy was approved before deposit IBANs existed."""
    from .policies import active_policy

    row = for_entity(session, entity.id)
    if row is None and active_policy(session, entity.id) is not None:
        row = assign(session, entity)
    return row


def entity_for(session: Session, iban: str) -> Entity | None:
    """The company a deposit IBAN belongs to (assigned IBANs only)."""
    row = session.scalar(select(VirtualIban).where(
        VirtualIban.iban == normalize_iban(iban), VirtualIban.status == "assigned"
    ))
    return session.get(Entity, row.entity_id) if row else None


def assign(session: Session, entity: Entity) -> VirtualIban | None:
    """Give the company its deposit IBAN (once). None when the inventory is empty: the company waits and the
    admin sees it on the support page; adding IBANs assigns them to waiting companies."""
    if current := for_entity(session, entity.id):
        return current
    _thaw(session)
    row = session.scalar(
        select(VirtualIban).where(VirtualIban.status == "available").order_by(VirtualIban.id).limit(1).with_for_update()
    )
    if row is None and is_simulated():
        # Simulation: issue a test IBAN so demos and tests work without loading an inventory
        row = VirtualIban(iban=f"SA91{entity.id:020d}")
        session.add(row)
    if row is None:
        return None
    row.status, row.entity_id, row.assigned_at = "assigned", entity.id, utcnow()
    session.flush()
    session.add(AuditLog(entity_id=entity.id, action="virtual_iban.assigned", details={"iban": row.iban[-4:]}))
    return row


def waiting(session: Session) -> list[Entity]:
    """Companies with an approved policy and no deposit IBAN yet."""
    assigned = select(VirtualIban.entity_id).where(VirtualIban.status == "assigned")
    approved = select(Policy.entity_id).where(Policy.status.in_([PolicyStatus.ACTIVE, PolicyStatus.ARCHIVED]))
    return list(session.scalars(
        select(Entity).where(Entity.id.in_(approved), Entity.id.not_in(assigned)).order_by(Entity.id)
    ))


def assign_waiting(session: Session) -> int:
    count = 0
    for entity in waiting(session):
        if assign(session, entity) is None:
            break
        count += 1
    return count


def release(session: Session, row: VirtualIban, user: User) -> None:
    """Take an IBAN back from a company: frozen for FREEZE_DAYS, then available again."""
    if row.status != "assigned":
        raise VirtualIbanError("الآيبان غير مخصّص لأي شركة")
    session.add(AuditLog(user_id=user.id, entity_id=row.entity_id, action="virtual_iban.released",
                         details={"iban": row.iban[-4:]}))
    row.last_entity_id, row.entity_id = row.entity_id, None
    row.status, row.released_at = "frozen", utcnow()


def stock(session: Session) -> dict[str, int]:
    _thaw(session)
    counts = dict(session.execute(select(VirtualIban.status, func.count()).group_by(VirtualIban.status)).all())
    return {k: counts.get(k, 0) for k in ("available", "assigned", "frozen")}


def inventory(session: Session) -> list[VirtualIban]:
    return list(session.scalars(select(VirtualIban).order_by(VirtualIban.status, VirtualIban.id)))
