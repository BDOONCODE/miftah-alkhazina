"""Policy lifecycle: draft -> pending approval -> active (or rejected), with archiving and audit."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..domain.periods import local_date, period_key
from ..domain.policy_rules import justification_error, protected_changes, validate_policy
from ..domain.types import ANCHORED_MONTHS, BucketSpec, CalcType, Frequency
from .accounts import DESTINATION_KINDS, AccountError, add_account, normalize_iban
from ..models import (
    AuditLog,
    BankAccount,
    Bucket,
    BucketPeriodState,
    Entity,
    PeriodClosure,
    Policy,
    PolicyBucket,
    PolicyStatus,
    Role,
    User,
    utcnow,
)

OPEN_STATUSES = (PolicyStatus.DRAFT, PolicyStatus.PENDING)


class PolicyError(ValueError):
    pass


@dataclass
class BucketInput:
    bucket_id: int | None
    name: str
    priority: int
    calc_type: CalcType
    value: int
    frequency: Frequency
    settlement_day: int | None
    destination: str
    protected: bool
    destination_account_id: int | None = None
    # "+ new IBAN" from the policy page: a new account is registered under the item's name
    new_destination_iban: str = ""
    new_destination_kind: str = "external"
    due_date: date | None = None  # next due date (quarterly, semiannual, annual)


def bucket_spec(pb: PolicyBucket) -> BucketSpec:
    return BucketSpec(
        bucket_id=pb.bucket_id,
        name=pb.name,
        priority=pb.priority,
        calc_type=pb.calc_type,
        value=pb.value,
        frequency=pb.frequency,
        settlement_day=pb.settlement_day,
        destination=str(pb.destination_account_id or pb.destination),
        protected=pb.protected,
        due_date=pb.due_date,
    )


def specs(policy: Policy | None) -> list[BucketSpec]:
    return [bucket_spec(pb) for pb in policy.buckets] if policy else []


def active_policy(session: Session, entity_id: int) -> Policy | None:
    return session.scalar(
        select(Policy).where(Policy.entity_id == entity_id, Policy.status == PolicyStatus.ACTIVE)
    )


def open_policy(session: Session, entity_id: int) -> Policy | None:
    """The draft or pending policy (at most one per entity)."""
    return session.scalar(
        select(Policy).where(Policy.entity_id == entity_id, Policy.status.in_(OPEN_STATUSES))
    )


def history(session: Session, entity_id: int) -> list[Policy]:
    return list(session.scalars(select(Policy).where(Policy.entity_id == entity_id).order_by(Policy.version.desc())))


def _audit(session: Session, user: User, policy: Policy, action: str, **details) -> None:
    session.add(
        AuditLog(
            user_id=user.id,
            entity_id=policy.entity_id,
            action=action,
            details={"policy_id": policy.id, "version": policy.version, **details},
        )
    )


def _draft_base(session: Session, entity_id: int) -> Policy | None:
    """A new draft starts from the latest active version, or from the latest rejected one if it is newer
    (so the accountant edits what the reviewer rejected instead of starting over)."""
    return session.scalar(
        select(Policy)
        .where(Policy.entity_id == entity_id, Policy.status.in_((PolicyStatus.ACTIVE, PolicyStatus.REJECTED)))
        .order_by(Policy.version.desc())
        .limit(1)
    )


def start_draft(session: Session, entity: Entity, user: User) -> Policy:
    """A new draft starting from the latest version (with the same item identities)."""
    if open_policy(session, entity.id):
        raise PolicyError("توجد مسودة أو سياسة بانتظار الاعتماد لهذه الشركة")
    last_version = session.scalar(select(func.max(Policy.version)).where(Policy.entity_id == entity.id)) or 0
    draft = Policy(entity_id=entity.id, version=last_version + 1, created_by=user.id)
    if current := _draft_base(session, entity.id):
        draft.notes = current.notes if current.status is PolicyStatus.REJECTED else ""
        draft.surplus_account_id = current.surplus_account_id
        draft.surplus_frequency = current.surplus_frequency
        draft.surplus_settlement_day = current.surplus_settlement_day
        draft.surplus_due_date = current.surplus_due_date
        draft.buckets = [
            PolicyBucket(
                bucket_id=pb.bucket_id,
                name=pb.name,
                priority=pb.priority,
                calc_type=pb.calc_type,
                value=pb.value,
                frequency=pb.frequency,
                settlement_day=pb.settlement_day,
                destination=pb.destination,
                destination_account_id=pb.destination_account_id,
                due_date=pb.due_date,
                protected=pb.protected,
            )
            for pb in current.buckets
        ]
    session.add(draft)
    session.flush()
    _audit(session, user, draft, "policy.draft_created")
    return draft


def save_draft(
    session: Session, policy: Policy, rows: list[BucketInput], notes: str, user: User,
    surplus_account_id: int | None = None,
    surplus_frequency: Frequency = Frequency.MONTHLY,
    surplus_day: int | None = None,
    surplus_due: date | None = None,
) -> None:
    if policy.status is not PolicyStatus.DRAFT:
        raise PolicyError("لا يمكن تعديل السياسة بعد إرسالها للاعتماد")

    owned = set(session.scalars(select(Bucket.id).where(Bucket.entity_id == policy.entity_id)))
    destinations = {
        a.id: a
        for a in session.scalars(
            select(BankAccount).where(
                BankAccount.entity_id == policy.entity_id,
                BankAccount.is_active.is_(True),
                BankAccount.kind.in_(DESTINATION_KINDS),
            )
        )
    }
    new_buckets: list[PolicyBucket] = []
    for row in rows:
        bucket_id = row.bucket_id
        if bucket_id is None:
            bucket = Bucket(entity_id=policy.entity_id)
            session.add(bucket)
            session.flush()
            bucket_id = bucket.id
        elif bucket_id not in owned:
            raise PolicyError("البند لا يتبع هذه الشركة")
        account = None
        if row.new_destination_iban.strip():
            account = _destination_from_iban(session, policy, user, row, destinations)
        elif row.destination_account_id is not None:
            account = destinations.get(row.destination_account_id)
            if account is None:
                raise PolicyError(f"مستفيد البند «{row.name.strip()}» ليس حسابًا نشطًا لهذه الشركة")
        new_buckets.append(
            PolicyBucket(
                bucket_id=bucket_id,
                name=row.name.strip(),
                priority=row.priority,
                calc_type=row.calc_type,
                value=row.value,
                frequency=row.frequency,
                settlement_day=row.settlement_day if row.frequency is Frequency.DAY_OF_MONTH else None,
                due_date=row.due_date if row.frequency in ANCHORED_MONTHS else None,
                destination=account.name if account else row.destination.strip(),
                destination_account_id=account.id if account else None,
                protected=row.protected,
            )
        )
    # Delete the old ones first because of the unique (policy_id, bucket_id) constraint
    policy.buckets.clear()
    session.flush()
    policy.buckets.extend(new_buckets)
    policy.notes = notes.strip()
    if surplus_account_id is not None:
        from .accounts import SURPLUS_KINDS

        target = session.get(BankAccount, surplus_account_id)
        if target is None or target.entity_id != policy.entity_id or not target.is_active or target.kind not in SURPLUS_KINDS:
            raise PolicyError("يُحوَّل الفائض إلى حساب نشط للشركة أو المالك فقط")
    policy.surplus_account_id = surplus_account_id
    if surplus_frequency not in SURPLUS_FREQUENCIES:
        raise PolicyError("اختر موعدًا صالحًا لتحويل الفائض")
    policy.surplus_frequency = surplus_frequency
    policy.surplus_settlement_day = surplus_day if surplus_frequency is Frequency.DAY_OF_MONTH else None
    policy.surplus_due_date = surplus_due if surplus_frequency in ANCHORED_MONTHS else None
    _audit(session, user, policy, "policy.saved", buckets=len(new_buckets))


def _destination_from_iban(session: Session, policy: Policy, user: User, row: BucketInput, destinations: dict) -> BankAccount:
    """New IBAN from the policy page: reuse the account if registered, otherwise register it under the item's name."""
    iban = normalize_iban(row.new_destination_iban)
    existing = session.scalar(
        select(BankAccount).where(BankAccount.entity_id == policy.entity_id, BankAccount.iban == iban)
    )
    if existing is not None:
        if existing.kind not in DESTINATION_KINDS:
            raise PolicyError(f"الآيبان {iban} مسجل كحساب دخل، ولا يصلح مستفيدًا لبند")
        if not existing.is_active:
            raise PolicyError(f"الآيبان {iban} يعود لحساب معطّل. فعّله من الحسابات البنكية")
        return existing
    kind = row.new_destination_kind if row.new_destination_kind in ("sub", "external") else "external"
    try:
        account = add_account(
            session, session.get(Entity, policy.entity_id), user,
            name=row.name.strip() or "حساب بند", bank_name="", iban=iban, kind=kind,
        )
    except AccountError as exc:
        raise PolicyError(f"البند «{row.name.strip()}»: {exc}") from exc
    destinations[account.id] = account
    return account


# Schedules for paying out the surplus (not immediate or daily: the surplus has to build up to cover deficits)
SURPLUS_FREQUENCIES = (
    Frequency.WEEKLY, Frequency.MONTHLY, Frequency.DAY_OF_MONTH,
    Frequency.QUARTERLY, Frequency.SEMIANNUAL, Frequency.ANNUAL,
)


def destination_errors(policy: Policy) -> list[str]:
    """Every item has a beneficiary its money transfers to, and the surplus schedule is complete."""
    errors = [f"البند «{pb.name}» ليس له مستفيد: اختر الآيبان الذي يُحوَّل إليه" for pb in policy.buckets
              if pb.destination_account_id is None]
    day = policy.surplus_settlement_day or 0
    if policy.surplus_frequency is Frequency.DAY_OF_MONTH and not 1 <= day <= 31:
        errors.append("حدد يوم تحويل الفائض من 1 إلى 31")
    if policy.surplus_frequency in ANCHORED_MONTHS and policy.surplus_due_date is None:
        errors.append("حدد تاريخ تحويل الفائض القادم")
    return errors


def source_errors(session: Session, entity_id: int) -> list[str]:
    """Before the first approval the company registers the account it will transfer its revenue from:
    deposits from registered accounts are split directly, anything else waits for the accountant."""
    from .accounts import INCOME_KINDS

    has_source = session.scalar(select(BankAccount.id).where(
        BankAccount.entity_id == entity_id, BankAccount.is_active, BankAccount.kind.in_(INCOME_KINDS)
    ).limit(1))
    return [] if has_source else ["سجّل في «الحسابات البنكية» الحساب الذي ستحوّل منه إيراداتك إلى آيبان الإيداع"]


def funded_in_current_period(session: Session, entity_id: int, buckets: list[BucketSpec], today: date) -> set[int]:
    by_id = {b.bucket_id: b for b in buckets}
    funded: set[int] = set()
    for state in session.scalars(select(BucketPeriodState).where(BucketPeriodState.entity_id == entity_id)):
        spec = by_id.get(state.bucket_id)
        if spec and state.funded > 0 and state.period_key == period_key(
            spec.frequency, today, spec.settlement_day, spec.due_date
        ):
            funded.add(state.bucket_id)
    return funded


def protected_changes_for(session: Session, policy: Policy, today: date | None = None) -> list[str]:
    current = active_policy(session, policy.entity_id)
    if current is None:
        return []
    old = specs(current)
    funded = funded_in_current_period(session, policy.entity_id, old, today or local_date(utcnow()))
    return protected_changes(old, specs(policy), funded)


def submit(session: Session, policy: Policy, user: User, justification: str) -> None:
    if policy.status is not PolicyStatus.DRAFT:
        raise PolicyError("السياسة ليست مسودة")
    if errors := validate_policy(specs(policy)) + destination_errors(policy) + source_errors(session, policy.entity_id):
        raise PolicyError("؛ ".join(errors))
    changes = protected_changes_for(session, policy)
    if error := justification_error(changes, justification):
        raise PolicyError(error)
    policy.status = PolicyStatus.PENDING
    policy.justification = justification.strip()
    policy.submitted_at = utcnow()
    _audit(session, user, policy, "policy.submitted", protected_changes=changes)


CHANGE_FIELDS = {
    "name": "الاسم",
    "priority": "الأولوية",
    "calc_type": "النوع",
    "value": "القيمة",
    "frequency": "الاستحقاق",
    "settlement_day": "يوم التحويل",
    "due_date": "تاريخ الاستحقاق",
    "destination_account_id": "المستفيد",
    "protected": "الحماية",
}
SURPLUS_FIELDS = ("surplus_account_id", "surplus_frequency", "surplus_settlement_day", "surplus_due_date")


@dataclass
class PolicyChanges:
    """What an open version changes against the active one, so the reviewer only looks at what to decide on."""

    first: bool  # no active policy yet: everything is new
    new: set[int]  # bucket ids added in this version
    changed: dict[int, tuple[PolicyBucket, list[str]]]  # bucket id -> (the active row, changed field names)
    removed: list[PolicyBucket]  # active rows dropped from this version
    surplus_changed: bool

    @property
    def any(self) -> bool:
        return bool(self.first or self.new or self.changed or self.removed or self.surplus_changed)


def changes_from_active(session: Session, policy: Policy) -> PolicyChanges:
    current = active_policy(session, policy.entity_id)
    if current is None or current.id == policy.id:
        first = current is None
        return PolicyChanges(first, {b.bucket_id for b in policy.buckets} if first else set(), {}, [], False)
    old = {b.bucket_id: b for b in current.buckets}
    new, changed = set(), {}
    for b in policy.buckets:
        prev = old.pop(b.bucket_id, None)
        if prev is None:
            new.add(b.bucket_id)
        elif fields := [f for f in CHANGE_FIELDS if getattr(prev, f) != getattr(b, f)]:
            changed[b.bucket_id] = (prev, fields)
    surplus = any(getattr(current, f) != getattr(policy, f) for f in SURPLUS_FIELDS)
    return PolicyChanges(False, new, changed, list(old.values()), surplus)


def beneficiaries(session: Session, entity_id: int) -> dict[int, BankAccount]:
    """The company's accounts by id, so the policy shows each beneficiary's name, kind and last digits
    (the reviewer has no access to the bank accounts page)."""
    return {a.id: a for a in session.scalars(select(BankAccount).where(BankAccount.entity_id == entity_id))}


SELF_APPROVAL = "self"
REVIEWER_APPROVAL = "reviewer"
APPROVAL_MODES = {SELF_APPROVAL: "أنا أعتمد السياسات بنفسي", REVIEWER_APPROVAL: "مراجع مستقل يعتمدها"}


def is_self_approval(session: Session, policy: Policy) -> bool:
    entity = session.get(Entity, policy.entity_id)
    return entity is not None and entity.approval_mode == SELF_APPROVAL


def can_decide(session: Session, policy: Policy, user: User) -> bool:
    """Can this user approve or reject the policy?"""
    if is_self_approval(session, policy):
        # The company chose self-approval: the accountant approves even as the creator
        return user.role in (Role.ACCOUNTANT, Role.APPROVER)
    # Reviewer approval: only the reviewer, never the policy's creator. The general admin never approves
    return user.role is Role.APPROVER and policy.created_by != user.id


def _check_decider(session: Session, policy: Policy, user: User) -> None:
    if policy.status is not PolicyStatus.PENDING:
        raise PolicyError("السياسة ليست بانتظار الاعتماد")
    if can_decide(session, policy, user):
        return
    if policy.created_by == user.id:
        raise PolicyError("لا يمكنك اعتماد سياسة أنشأتها بنفسك. غيّر طريقة الاعتماد من «بيانات الشركة» إذا أردت الاعتماد بنفسك")
    raise PolicyError("الاعتماد للمراجع فقط")


def approve(session: Session, policy: Policy, user: User, note: str = "") -> None:
    _check_decider(session, policy, user)
    if errors := validate_policy(specs(policy)):  # safety net in case something changed after submission
        raise PolicyError("؛ ".join(errors))

    previous = active_policy(session, policy.entity_id)
    removed: list[int] = []
    if previous:
        previous.status = PolicyStatus.ARCHIVED
        kept = {pb.bucket_id for pb in policy.buckets}
        removed = [pb.bucket_id for pb in previous.buckets if pb.bucket_id not in kept]
        _close_removed_buckets(session, policy, removed)

    session.flush()  # archive before activating
    policy.status = PolicyStatus.ACTIVE
    policy.decided_by = user.id
    policy.decided_at = utcnow()
    policy.decision_note = note.strip()
    _audit(
        session, user, policy, "policy.approved", removed_buckets=removed, self_approved=policy.created_by == user.id
    )
    from . import virtual_ibans

    # The first approved policy gives the company its deposit IBAN (it waits if the inventory is empty)
    virtual_ibans.assign(session, session.get(Entity, policy.entity_id))


def set_approval_mode(session: Session, entity: Entity, user: User, mode: str) -> None:
    if mode not in APPROVAL_MODES:
        raise PolicyError("طريقة اعتماد غير معروفة")
    if mode == entity.approval_mode:
        return
    entity.approval_mode = mode
    session.add(
        AuditLog(user_id=user.id, entity_id=entity.id, action="company.approval_mode_changed", details={"mode": mode})
    )


def _close_removed_buckets(session: Session, policy: Policy, bucket_ids: list[int]) -> None:
    """A removed item's period closes immediately and its final deficit is recorded."""
    for bucket_id in bucket_ids:
        state = session.get(BucketPeriodState, bucket_id)
        if state is None:
            continue
        deficit = max(0, state.target - state.funded) if state.target else state.carried_deficit
        session.add(
            PeriodClosure(
                entity_id=state.entity_id,
                bucket_id=bucket_id,
                period_key=state.period_key,
                target=state.target,
                funded=state.funded,
                closed_deficit=deficit,
                closed_by_policy_id=policy.id,
            )
        )
        session.delete(state)


def reject(session: Session, policy: Policy, user: User, note: str) -> None:
    _check_decider(session, policy, user)
    if not note.strip():
        raise PolicyError("اكتب سبب الرفض")
    policy.status = PolicyStatus.REJECTED
    policy.decided_by = user.id
    policy.decided_at = utcnow()
    policy.decision_note = note.strip()
    _audit(session, user, policy, "policy.rejected", note=note.strip())


def withdraw(session: Session, policy: Policy, user: User) -> None:
    """The accountant pulls the policy back from the reviewer as a draft to edit it."""
    if policy.status is not PolicyStatus.PENDING:
        raise PolicyError("السياسة ليست بانتظار الاعتماد")
    policy.status = PolicyStatus.DRAFT
    policy.submitted_at = None
    _audit(session, user, policy, "policy.withdrawn")


def discard_draft(session: Session, policy: Policy, user: User) -> None:
    if policy.status is not PolicyStatus.DRAFT:
        raise PolicyError("السياسة ليست مسودة")
    _audit(session, user, policy, "policy.draft_discarded")
    session.delete(policy)
