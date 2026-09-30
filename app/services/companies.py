"""تسجيل الشركات والمراجعين: المحاسب يسجّل الشركة ومين يراجع معه."""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuditLog, Dashboard, Entity, Role, User
from ..security import hash_password
from .accounts import AccountError, add_account
from .dashboards import apply_default_layout

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{3,64}$")


class CompanyError(ValueError):
    pass


@dataclass
class CompanyInput:
    name: str
    cr_number: str = ""
    vat_number: str = ""
    bank_name: str = ""
    iban: str = ""
    contact_name: str = ""
    contact_phone: str = ""


@dataclass
class ReviewerInput:
    full_name: str
    username: str
    password: str


def _validate_company(data: CompanyInput) -> None:
    if not data.name.strip():
        raise CompanyError("اسم الشركة مطلوب")
    iban = data.iban.replace(" ", "").upper()
    if iban and not re.fullmatch(r"SA\d{22}", iban):
        raise CompanyError("رقم الآيبان السعودي يبدأ بـSA وبعده 22 رقمًا")


def _apply(entity: Entity, data: CompanyInput) -> None:
    entity.name = data.name.strip()
    entity.cr_number = data.cr_number.strip()
    entity.vat_number = data.vat_number.strip()
    entity.contact_name = data.contact_name.strip()
    entity.contact_phone = data.contact_phone.strip()


def create_user(
    session: Session, *, full_name: str, username: str, password: str, role: Role, actor: User, enforce_rules: bool = True
) -> User:
    username = username.strip()
    if not full_name.strip():
        raise CompanyError("الاسم الكامل مطلوب")
    if not USERNAME_RE.match(username):
        raise CompanyError("اسم المستخدم بالإنجليزي أو أرقام (3 أحرف على الأقل، بدون مسافات)")
    if session.scalar(select(User).where(User.username == username)):
        raise CompanyError(f"اسم المستخدم «{username}» مستخدم مسبقًا")
    try:
        password_hash = hash_password(password, enforce_rules=enforce_rules)
    except ValueError as exc:
        raise CompanyError(str(exc)) from exc
    user = User(
        username=username,
        full_name=full_name.strip(),
        password_hash=password_hash,
        role=role,
        must_change_password=True,
    )
    session.add(user)
    session.flush()
    session.add(AuditLog(user_id=actor.id, action="user.create", details={"username": username, "role": role.value}))
    return user


def register_company(
    session: Session,
    accountant: User,
    company: CompanyInput,
    reviewer: ReviewerInput | None,
    approval_mode: str = "reviewer",
    *,
    enforce_password_rules: bool = True,
) -> Entity:
    """الاعتماد الذاتي ما يحتاج مراجع؛ الاعتماد بمراجع يحتاج بيانات المراجع."""
    _validate_company(company)
    if approval_mode not in ("self", "reviewer"):
        raise CompanyError("طريقة اعتماد غير معروفة")
    if approval_mode == "reviewer" and reviewer is None:
        raise CompanyError("حدد المراجع اللي يعتمد السياسات، أو اختر إنك تعتمدها بنفسك")
    entity = Entity(created_by=accountant.id, approval_mode=approval_mode)
    _apply(entity, company)
    session.add(entity)
    session.flush()

    if accountant.role is not Role.ADMIN:  # المدير يشوف كل الشركات أصلًا
        entity.users.append(accountant)
    if company.iban.strip():
        # آيبان التسجيل هو الحساب المجمّع اللي تتجمع فيه الإيرادات
        try:
            add_account(
                session, entity, accountant,
                name="الحساب المجمّع", bank_name=company.bank_name, iban=company.iban, kind="pool",
            )
        except AccountError as exc:
            raise CompanyError(str(exc)) from exc
    reviewer_user = None
    if reviewer is not None:
        reviewer_user = create_user(
            session,
            full_name=reviewer.full_name,
            username=reviewer.username,
            password=reviewer.password,
            role=Role.APPROVER,
            actor=accountant,
            enforce_rules=enforce_password_rules,
        )
        entity.users.append(reviewer_user)
    dashboard = Dashboard(entity_id=entity.id, title=f"لوحة {entity.name}", updated_by=accountant.id)
    apply_default_layout(dashboard)
    session.add(dashboard)
    session.add(
        AuditLog(
            user_id=accountant.id,
            entity_id=entity.id,
            action="company.registered",
            details={
                "name": entity.name,
                "reviewer": reviewer_user.username if reviewer_user else None,
                "approval_mode": approval_mode,
            },
        )
    )
    return entity


def update_company(session: Session, entity: Entity, data: CompanyInput, actor: User) -> None:
    _validate_company(data)
    _apply(entity, data)
    session.add(AuditLog(user_id=actor.id, entity_id=entity.id, action="company.updated", details={"name": entity.name}))


def add_reviewer(session: Session, entity: Entity, reviewer: ReviewerInput, actor: User) -> User:
    user = create_user(
        session,
        full_name=reviewer.full_name,
        username=reviewer.username,
        password=reviewer.password,
        role=Role.APPROVER,
        actor=actor,
    )
    entity.users.append(user)
    session.add(
        AuditLog(user_id=actor.id, entity_id=entity.id, action="company.reviewer_added", details={"username": user.username})
    )
    return user
