"""أوامر الإدارة من سطر الأوامر.

    python -m app.cli create-admin --username admin --full-name "مدير النظام"
"""
from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select

from .db import SessionLocal
from .models import AuditLog, Entity, Role, User
from .security import hash_password


def create_admin(username: str, full_name: str) -> None:
    with SessionLocal() as session:
        if session.scalar(select(User).where(User.username == username)):
            sys.exit(f"اسم المستخدم «{username}» موجود مسبقًا")
        password = getpass.getpass("كلمة المرور: ")
        if password != getpass.getpass("أعد كتابة كلمة المرور: "):
            sys.exit("كلمتا المرور غير متطابقتين")
        try:
            password_hash = hash_password(password)
        except ValueError as exc:
            sys.exit(str(exc))
        user = User(username=username, full_name=full_name, password_hash=password_hash, role=Role.ADMIN)
        session.add(user)
        session.flush()
        session.add(AuditLog(user_id=user.id, action="user.create", details={"username": username, "role": "admin"}))
        session.commit()
        print(f"تم إنشاء المدير «{username}»")


# حسابات تجريبية. محليًا بهذي الكلمات؛ على الخادم تُستبدل كلها بـDEMO_PASSWORD.
# مراجع التجربة اليدوية يُسجَّل من شاشة الشركة: demo-rev بكلمة مؤقتة demo-rev-temp ← demo-rev-pass
DEMO_USERS = [
    ("demo-admin", "مدير تجريبي", Role.ADMIN, "demo-admin-pass"),
    ("demo-acc", "محاسب تجريبي", Role.ACCOUNTANT, "demo-acc-pass"),
]


def seed_demo(password: str | None = None) -> None:
    """محليًا بكلمات المرور المكتوبة فوق. على الخادم (bootstrap) بكلمة DEMO_PASSWORD وبدون مدير تجريبي."""
    from .config import IS_PRODUCTION

    if IS_PRODUCTION and not password:
        sys.exit("seed-demo على الخادم يحتاج DEMO_PASSWORD")
    with SessionLocal() as session:
        for username, full_name, role, default_password in DEMO_USERS:
            if IS_PRODUCTION and role is Role.ADMIN:
                continue  # مدير الإنتاج يجي من ADMIN_USERNAME
            if not session.scalar(select(User).where(User.username == username)):
                session.add(
                    User(username=username, full_name=full_name, password_hash=hash_password(password or default_password, enforce_rules=False), role=role)
                )
        session.commit()
        if not session.scalar(select(Entity).where(Entity.name == DEMO_COMPANY)):
            _seed_demo_company(session, password)
            session.commit()
    print("الحسابات والشركة التجريبية جاهزة")


def bootstrap() -> None:
    """يشتغل مع كل تشغيل للخادم (ما في طرفية في الاستضافة المجانية):
    ينشئ المدير الأول من ADMIN_USERNAME/ADMIN_PASSWORD لو ما في مستخدمين،
    ويضيف الشركة التجريبية لو DEMO_SEED=1.
    """
    import os

    with SessionLocal() as session:
        has_users = session.scalar(select(User.id).limit(1)) is not None
        username, password = os.environ.get("ADMIN_USERNAME", "").strip(), os.environ.get("ADMIN_PASSWORD", "")
        if not has_users and username and password:
            user = User(
                username=username,
                full_name=os.environ.get("ADMIN_FULL_NAME", "مدير النظام"),
                password_hash=hash_password(password, enforce_rules=False),
                role=Role.ADMIN,
                must_change_password=True,  # كلمة البيئة مؤقتة، تتغيّر أول دخول
            )
            session.add(user)
            session.flush()
            session.add(AuditLog(user_id=user.id, action="user.create", details={"username": username, "role": "admin"}))
            session.commit()
            print(f"أُنشئ المدير «{username}»")
        _recover_account(session)
    if os.environ.get("DEMO_SEED") == "1":
        seed_demo(os.environ.get("DEMO_PASSWORD") or None)


def _recover_account(session) -> None:
    """استرجاع حساب مقفول من إعدادات الاستضافة (لما ما في مدير يقدر يدخل).

    RECOVER_USERNAME + RECOVER_PASSWORD: تُعطى الحساب كلمة مؤقتة، ويتفعّل، ويُطلب تغييرها أول دخول.
    RECOVER_AS_ADMIN=1: يرقّى الحساب لمدير كمان. بعد الدخول تُحذف هذي المتغيرات من الإعدادات.
    """
    import os

    from sqlalchemy import func

    username = os.environ.get("RECOVER_USERNAME", "").strip()
    password = os.environ.get("RECOVER_PASSWORD", "")
    if not (username and password):
        return
    user = session.scalar(select(User).where(func.lower(User.username) == username.lower()))
    if user is None:
        print(f"الاسترجاع: ما في مستخدم باسم «{username}»، الأسماء الموجودة: "
              + "، ".join(session.scalars(select(User.username))))
        return
    user.password_hash = hash_password(password, enforce_rules=False)
    user.must_change_password = True
    user.is_active = True
    if os.environ.get("RECOVER_AS_ADMIN") == "1":
        user.role = Role.ADMIN
    session.add(AuditLog(user_id=user.id, action="user.recovered", details={"username": user.username}))
    session.commit()
    print(f"الاسترجاع: تغيّرت كلمة مرور «{user.username}» ({user.role.value}). احذف متغيرات RECOVER_* من الإعدادات")


DEMO_COMPANY = "شركة تجريبية"
DEMO_REVIEWER = ("demo-rev2", "مراجع تجريبي", "demo-rev2-pass")


def _seed_demo_company(session, password: str | None = None) -> None:
    """شركة بسياسة معتمدة وخمس معاملات في الشهر الحالي، عشان اللوحة والتقارير يكون فيها بيانات."""
    from datetime import datetime, timedelta

    from .domain.periods import RIYADH
    from .domain.types import CalcType, Frequency
    from .services import companies, policies, transactions

    accountant = session.scalar(select(User).where(User.username == "demo-acc"))
    username, full_name, default_password = DEMO_REVIEWER
    entity = companies.register_company(
        session, accountant, companies.CompanyInput(name=DEMO_COMPANY, bank_name="بنك تجريبي"),
        companies.ReviewerInput(full_name, username, password or default_password),
        enforce_password_rules=False,  # كلمة تجريبية من الإعدادات
    )
    reviewer = session.scalar(select(User).where(User.username == username))
    reviewer.must_change_password = False
    draft = policies.start_draft(session, entity, accountant)
    policies.save_draft(session, draft, [
        # الضريبة تتحوّل فورًا لحسابها، والإيجار والرواتب والأرباح تتجمّع وتتحوّل يوم استحقاقها
        policies.BucketInput(None, "الضريبة", 1, CalcType.PERCENTAGE, 1500, Frequency.IMMEDIATE, None, "", True,
                             new_destination_iban="SA1100000000000000000001", new_destination_kind="sub"),
        policies.BucketInput(None, "الإيجار", 2, CalcType.FIXED_AMOUNT, 1_000_000, Frequency.DAY_OF_MONTH, 25, "", False,
                             new_destination_iban="SA1100000000000000000003", new_destination_kind="external"),
        policies.BucketInput(None, "الرواتب", 3, CalcType.FIXED_AMOUNT, 1_500_000, Frequency.MONTHLY, None, "", True,
                             new_destination_iban="SA1100000000000000000004", new_destination_kind="sub"),
        policies.BucketInput(None, "الأرباح", 4, CalcType.PERCENTAGE, 500, Frequency.MONTHLY, None, "", False,
                             new_destination_iban="SA1100000000000000000002", new_destination_kind="sub"),
    ], "سياسة تجريبية", accountant)
    policies.submit(session, draft, accountant, "")
    policies.approve(session, draft, reviewer)
    session.flush()

    now = datetime.now(RIYADH)
    start = now.replace(day=1, hour=10, minute=0, second=0, microsecond=0)
    span = max(1, (now - start).days)
    for i, amount in enumerate([400_000, 900_000, 300_000, 1_200_000, 650_000]):  # بالهللة
        when = min(now, start + timedelta(days=span * i // 5, hours=i))
        transactions.record_transaction(session, entity, accountant, amount, when, note=f"دفعة عميل {i + 1}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin", help="إنشاء مستخدم مدير")
    admin.add_argument("--username", required=True)
    admin.add_argument("--full-name", required=True)
    sub.add_parser("seed-demo", help="حسابات تجريبية للتطوير المحلي")
    sub.add_parser("bootstrap", help="تهيئة الخادم من متغيرات البيئة")
    args = parser.parse_args()
    if args.command == "create-admin":
        create_admin(args.username, args.full_name)
    elif args.command == "seed-demo":
        seed_demo()
    elif args.command == "bootstrap":
        bootstrap()


if __name__ == "__main__":
    main()
