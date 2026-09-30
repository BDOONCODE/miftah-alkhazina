"""دورة حياة السياسة: مسودة ← بانتظار الاعتماد ← نشطة (أو مرفوضة)، مع الأرشفة والتدقيق."""
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
    # «+ آيبان جديد» من صفحة السياسة: يتسجّل حساب جديد باسم البند
    new_destination_iban: str = ""
    new_destination_kind: str = "sub"
    due_date: date | None = None  # تاريخ الاستحقاق القادم (ربع/نصف سنوي، سنوي)


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
    """المسودة أو السياسة بانتظار الاعتماد (وحدة بالكثير لكل كيان)."""
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
    """المسودة الجديدة تبدأ من آخر نسخة نشطة، أو من آخر نسخة مرفوضة لو كانت أحدث
    (عشان المحاسب يعدّل على اللي رفضه المراجع بدل ما يبدأ من الصفر)."""
    return session.scalar(
        select(Policy)
        .where(Policy.entity_id == entity_id, Policy.status.in_((PolicyStatus.ACTIVE, PolicyStatus.REJECTED)))
        .order_by(Policy.version.desc())
        .limit(1)
    )


def start_draft(session: Session, entity: Entity, user: User) -> Policy:
    """مسودة جديدة تبدأ من آخر نسخة (بنفس هويات البنود)."""
    if open_policy(session, entity.id):
        raise PolicyError("فيه مسودة أو سياسة بانتظار الاعتماد لهذي الشركة")
    last_version = session.scalar(select(func.max(Policy.version)).where(Policy.entity_id == entity.id)) or 0
    draft = Policy(entity_id=entity.id, version=last_version + 1, created_by=user.id)
    if current := _draft_base(session, entity.id):
        draft.notes = current.notes if current.status is PolicyStatus.REJECTED else ""
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


def save_draft(session: Session, policy: Policy, rows: list[BucketInput], notes: str, user: User) -> None:
    if policy.status is not PolicyStatus.DRAFT:
        raise PolicyError("ما يمكن تعديل سياسة بعد إرسالها للاعتماد")

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
            raise PolicyError("بند غير تابع لهذي الشركة")
        account = None
        if row.new_destination_iban.strip():
            account = _destination_from_iban(session, policy, user, row, destinations)
        elif row.destination_account_id is not None:
            account = destinations.get(row.destination_account_id)
            if account is None:
                raise PolicyError(f"وجهة البند «{row.name.strip()}» مو حساب نشط (مجمّع أو فرعي أو خارجي) لهذي الشركة")
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
    # نمسح القديمة أول عشان قيد (policy_id, bucket_id) الفريد
    policy.buckets.clear()
    session.flush()
    policy.buckets.extend(new_buckets)
    policy.notes = notes.strip()
    _audit(session, user, policy, "policy.saved", buckets=len(new_buckets))


def _destination_from_iban(session: Session, policy: Policy, user: User, row: BucketInput, destinations: dict) -> BankAccount:
    """آيبان جديد من صفحة السياسة: نستخدم الحساب لو مسجّل، وإلا نسجّله باسم البند."""
    iban = normalize_iban(row.new_destination_iban)
    existing = session.scalar(
        select(BankAccount).where(BankAccount.entity_id == policy.entity_id, BankAccount.iban == iban)
    )
    if existing is not None:
        if existing.kind not in DESTINATION_KINDS:
            raise PolicyError(f"الآيبان {iban} مسجّل كحساب مصدر إيراد، ما يصلح وجهة لبند")
        if not existing.is_active:
            raise PolicyError(f"الآيبان {iban} لحساب معطّل. فعّله من الحسابات البنكية")
        return existing
    kind = row.new_destination_kind if row.new_destination_kind in ("sub", "external") else "sub"
    try:
        account = add_account(
            session, session.get(Entity, policy.entity_id), user,
            name=row.name.strip() or "حساب بند", bank_name="", iban=iban, kind=kind,
        )
    except AccountError as exc:
        raise PolicyError(f"البند «{row.name.strip()}»: {exc}") from exc
    destinations[account.id] = account
    return account


def destination_errors(policy: Policy) -> list[str]:
    """كل بند لازم له آيبان وجهة (المجمّع أو حساب فرعي أو خارجي)."""
    return [f"البند «{pb.name}» ما له آيبان وجهة" for pb in policy.buckets if pb.destination_account_id is None]


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
        raise PolicyError("السياسة مو مسودة")
    if errors := validate_policy(specs(policy)) + destination_errors(policy):
        raise PolicyError("؛ ".join(errors))
    changes = protected_changes_for(session, policy)
    if error := justification_error(changes, justification):
        raise PolicyError(error)
    policy.status = PolicyStatus.PENDING
    policy.justification = justification.strip()
    policy.submitted_at = utcnow()
    _audit(session, user, policy, "policy.submitted", protected_changes=changes)


SELF_APPROVAL = "self"
REVIEWER_APPROVAL = "reviewer"
APPROVAL_MODES = {SELF_APPROVAL: "أنا أعتمد السياسات بنفسي", REVIEWER_APPROVAL: "مراجع مستقل يعتمدها"}


def is_self_approval(session: Session, policy: Policy) -> bool:
    entity = session.get(Entity, policy.entity_id)
    return entity is not None and entity.approval_mode == SELF_APPROVAL


def can_decide(session: Session, policy: Policy, user: User) -> bool:
    """هل يقدر هذا المستخدم يعتمد أو يرفض السياسة؟"""
    if is_self_approval(session, policy):
        # الشركة اختارت الاعتماد الذاتي: المحاسب/المدير يعتمد حتى لو هو المنشئ
        return user.role in (Role.ACCOUNTANT, Role.ADMIN, Role.APPROVER)
    return user.role in (Role.APPROVER, Role.ADMIN) and policy.created_by != user.id


def _check_decider(session: Session, policy: Policy, user: User) -> None:
    if policy.status is not PolicyStatus.PENDING:
        raise PolicyError("السياسة مو بانتظار الاعتماد")
    if can_decide(session, policy, user):
        return
    if policy.created_by == user.id:
        raise PolicyError("ما يمكن تعتمد سياسة أنشأتها بنفسك. غيّر طريقة الاعتماد من «بيانات الشركة» لو تبي تعتمد بنفسك")
    raise PolicyError("الاعتماد للمراجع فقط")


def approve(session: Session, policy: Policy, user: User, note: str = "") -> None:
    _check_decider(session, policy, user)
    if errors := validate_policy(specs(policy)):  # احتياط لو تغيّر شي بعد الإرسال
        raise PolicyError("؛ ".join(errors))

    previous = active_policy(session, policy.entity_id)
    removed: list[int] = []
    if previous:
        previous.status = PolicyStatus.ARCHIVED
        kept = {pb.bucket_id for pb in policy.buckets}
        removed = [pb.bucket_id for pb in previous.buckets if pb.bucket_id not in kept]
        _close_removed_buckets(session, policy, removed)

    session.flush()  # الأرشفة قبل التفعيل
    policy.status = PolicyStatus.ACTIVE
    policy.decided_by = user.id
    policy.decided_at = utcnow()
    policy.decision_note = note.strip()
    _audit(
        session, user, policy, "policy.approved", removed_buckets=removed, self_approved=policy.created_by == user.id
    )


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
    """البند المحذوف تنقفل فترته فورًا ويُسجَّل عجزه النهائي."""
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
    """المحاسب يسحب السياسة من المراجع ويرجّعها مسودة عشان يعدّل عليها."""
    if policy.status is not PolicyStatus.PENDING:
        raise PolicyError("السياسة مو بانتظار الاعتماد")
    policy.status = PolicyStatus.DRAFT
    policy.submitted_at = None
    _audit(session, user, policy, "policy.withdrawn")


def discard_draft(session: Session, policy: Policy, user: User) -> None:
    if policy.status is not PolicyStatus.DRAFT:
        raise PolicyError("السياسة مو مسودة")
    _audit(session, user, policy, "policy.draft_discarded")
    session.delete(policy)
