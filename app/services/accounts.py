"""حسابات الشركة البنكية: مصادر الإيراد، والحساب المجمّع، والحسابات الفرعية، وحسابات الجهات الخارجية."""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AccountKind, AuditLog, BankAccount, Entity, User

KIND_LABELS = {
    AccountKind.SOURCE.value: "مصدر إيراد",
    AccountKind.POOL.value: "مجمّع",
    AccountKind.SUB.value: "حساب آخر للشركة",
    AccountKind.EXTERNAL.value: "خارجي (جهة ثانية)",
}
KIND_HINTS = {
    AccountKind.SOURCE.value: "تدخل فيه إيرادات الشركة: نقاط البيع، مدى/فيزا، تطبيقات التوصيل",
    AccountKind.POOL.value: "تتجمع فيه كل الإيرادات، ومنه يتقسّم المبلغ. حساب واحد لكل شركة",
    AccountKind.SUB.value: "حساب ثاني تملكه الشركة، مثل حساب أرباح المالك أو حساب الرواتب",
    AccountKind.EXTERNAL.value: "حساب جهة تتحوّل لها فلوس: مانح الامتياز، المؤجر، مورد",
}
# الأنواع اللي تصلح وجهة لبند في السياسة
DESTINATION_KINDS = (AccountKind.POOL, AccountKind.SUB, AccountKind.EXTERNAL)
IBAN_RE = re.compile(r"^SA\d{22}$")


class AccountError(ValueError):
    pass


def normalize_iban(iban: str) -> str:
    return re.sub(r"\s+", "", iban).upper()


def accounts_for(session: Session, entity_id: int, *, active_only: bool = False) -> list[BankAccount]:
    stmt = select(BankAccount).where(BankAccount.entity_id == entity_id)
    if active_only:
        stmt = stmt.where(BankAccount.is_active.is_(True))
    order = {AccountKind.POOL: 0, AccountKind.SOURCE: 1, AccountKind.SUB: 2, AccountKind.EXTERNAL: 3}
    return sorted(session.scalars(stmt), key=lambda a: (order[a.kind], a.name))


def pool_account(session: Session, entity_id: int) -> BankAccount | None:
    return session.scalar(
        select(BankAccount).where(
            BankAccount.entity_id == entity_id, BankAccount.kind == AccountKind.POOL, BankAccount.is_active.is_(True)
        )
    )


def add_account(
    session: Session, entity: Entity, user: User, *, name: str, bank_name: str, iban: str, kind: str
) -> BankAccount:
    name, iban = name.strip(), normalize_iban(iban)
    if not name:
        raise AccountError("اسم الحساب مطلوب")
    if not IBAN_RE.match(iban):
        raise AccountError("الآيبان السعودي يبدأ بـSA وبعده 22 رقمًا")
    try:
        account_kind = AccountKind(kind)
    except ValueError as exc:
        raise AccountError("اختر نوع الحساب") from exc
    if session.scalar(select(BankAccount).where(BankAccount.entity_id == entity.id, BankAccount.iban == iban)):
        raise AccountError("هذا الآيبان مسجّل لهذي الشركة")
    if account_kind is AccountKind.POOL and pool_account(session, entity.id):
        raise AccountError("الشركة لها حساب مجمّع. عطّل الحالي أول لو تبي تغيّره")

    account = BankAccount(entity_id=entity.id, name=name, bank_name=bank_name.strip(), iban=iban, kind=account_kind)
    session.add(account)
    session.flush()
    session.add(
        AuditLog(
            user_id=user.id,
            entity_id=entity.id,
            action="account.added",
            details={"name": name, "kind": account_kind.value, "iban": iban[-4:]},
        )
    )
    return account


def toggle_account(session: Session, account: BankAccount, user: User) -> None:
    if not account.is_active and account.kind is AccountKind.POOL and pool_account(session, account.entity_id):
        raise AccountError("فيه حساب مجمّع نشط. عطّله أول")
    account.is_active = not account.is_active
    session.add(
        AuditLog(
            user_id=user.id,
            entity_id=account.entity_id,
            action="account.toggled",
            details={"name": account.name, "active": account.is_active},
        )
    )


def find_by_iban(session: Session, iban: str) -> BankAccount | None:
    """الحساب اللي وصل له المبلغ (لإشعارات البنك): مصدر إيراد أو مجمّع فقط."""
    return session.scalar(
        select(BankAccount).where(
            BankAccount.iban == normalize_iban(iban),
            BankAccount.is_active.is_(True),
            BankAccount.kind.in_((AccountKind.SOURCE, AccountKind.POOL)),
        )
    )
