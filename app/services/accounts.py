"""Company bank accounts: revenue sources, the pool account, the company's other accounts, and third parties."""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AccountKind, AuditLog, BankAccount, Entity, User

KIND_LABELS = {
    AccountKind.SOURCE.value: "مصدر إيراد",
    AccountKind.POOL.value: "مجمّع",
    AccountKind.SUB.value: "حساب للمالك أو الشركة",
    AccountKind.EXTERNAL.value: "جهة خارجية",
}
KIND_HINTS = {
    AccountKind.SOURCE.value: "حساب تُودَع فيه المبيعات مباشرة: نقاط البيع، ومدى/فيزا، وتطبيقات التوصيل. يمكن إضافة أكثر من حساب",
    AccountKind.POOL.value: "حساب رئيسي تُودَع فيه جميع المبالغ، ويُختار بدلًا من ربط حسابات نقاط البيع كلًّا على حدة. حساب واحد لكل شركة",
    AccountKind.SUB.value: "حساب آخر يملكه المالك أو الشركة، مثل حساب الأرباح أو حساب الرواتب",
    AccountKind.EXTERNAL.value: "حساب جهة تُحوَّل إليها المبالغ: مانح الامتياز، أو المؤجر، أو المورد",
}
# Income accounts: where money comes in (managed on the bank accounts page)
INCOME_KINDS = (AccountKind.SOURCE, AccountKind.POOL)
# Beneficiary accounts: where money goes out to (added from the policy page)
BENEFICIARY_KINDS = (AccountKind.SUB, AccountKind.EXTERNAL)
# Account kinds that can be an item's beneficiary (or the surplus destination): beneficiaries only
DESTINATION_KINDS = BENEFICIARY_KINDS
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
        raise AccountError("يبدأ الآيبان السعودي بـSA ويليه 22 رقمًا")
    try:
        account_kind = AccountKind(kind)
    except ValueError as exc:
        raise AccountError("اختر نوع الحساب") from exc
    if session.scalar(select(BankAccount).where(BankAccount.entity_id == entity.id, BankAccount.iban == iban)):
        raise AccountError("هذا الآيبان مسجل لهذه الشركة")
    if account_kind is AccountKind.POOL and pool_account(session, entity.id):
        raise AccountError("للشركة حساب مجمّع بالفعل. عطّل الحساب الحالي أولًا إذا أردت تغييره")

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
        raise AccountError("يوجد حساب مجمّع نشط. عطّله أولًا")
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
    """The account the money arrived in (for bank notifications): a revenue source or the pool only."""
    return session.scalar(
        select(BankAccount).where(
            BankAccount.iban == normalize_iban(iban),
            BankAccount.is_active.is_(True),
            BankAccount.kind.in_((AccountKind.SOURCE, AccountKind.POOL)),
        )
    )
