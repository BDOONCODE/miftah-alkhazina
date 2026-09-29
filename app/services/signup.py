"""التسجيل الذاتي، تأكيد البريد، واستعادة كلمة المرور.

الروابط موقّعة بمفتاح الخادم (itsdangerous) ولها مدة صلاحية، فما نحتاج جدول رموز.
رابط استعادة كلمة المرور يستخدم مرة وحدة: فيه بصمة من كلمة المرور الحالية،
فأول ما تتغيّر يبطل الرابط.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from html import escape

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import SECRET_KEY
from ..mailer import send_email
from ..models import AuditLog, Role, User
from ..security import hash_password

VERIFY_MAX_AGE = 48 * 3600
RESET_MAX_AGE = 3600
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ACCOUNT_TYPES = {"office": "مكتب محاسبة", "business": "صاحب شركة"}

_verify = URLSafeTimedSerializer(SECRET_KEY, salt="verify-email")
_reset = URLSafeTimedSerializer(SECRET_KEY, salt="reset-password")


class SignupError(ValueError):
    pass


@dataclass
class SignupInput:
    full_name: str
    email: str
    password: str
    confirm: str
    account_type: str


def normalize_email(email: str) -> str:
    return email.strip().lower()


def find_by_login(session: Session, login: str) -> User | None:
    """الدخول باسم المستخدم أو البريد، بدون حساسية للأحرف."""
    value = login.strip().lower()
    return session.scalar(
        select(User).where((func.lower(User.username) == value) | (func.lower(User.email) == value))
    )


def register(session: Session, data: SignupInput) -> User:
    email = normalize_email(data.email)
    if not data.full_name.strip():
        raise SignupError("الاسم مطلوب")
    if not EMAIL_RE.match(email) or len(email) > 64:
        raise SignupError("البريد الإلكتروني غير صحيح")
    if data.account_type not in ACCOUNT_TYPES:
        raise SignupError("اختر نوع الحساب")
    if data.confirm and data.password != data.confirm:
        raise SignupError("كلمتا المرور غير متطابقتين")
    if find_by_login(session, email):
        raise SignupError("هذا البريد مسجّل. سجّل دخولك، أو استخدم «نسيت كلمة المرور»")
    try:
        password_hash = hash_password(data.password)
    except ValueError as exc:
        raise SignupError(str(exc)) from exc

    user = User(
        username=email,
        email=email,
        full_name=data.full_name.strip(),
        password_hash=password_hash,
        role=Role.ACCOUNTANT,  # الاثنين يسجّلون شركات ويبنون سياسات
        account_type=data.account_type,
        email_verified=False,
    )
    session.add(user)
    session.flush()
    session.add(AuditLog(user_id=user.id, action="user.signup", details={"email": email, "type": data.account_type}))
    return user


# ------------------------------------------------------------------ تأكيد البريد


def verification_token(user: User) -> str:
    return _verify.dumps({"uid": user.id, "email": user.email})


def verify_email(session: Session, token: str) -> User:
    try:
        data = _verify.loads(token, max_age=VERIFY_MAX_AGE)
    except SignatureExpired as exc:
        raise SignupError("انتهت صلاحية رابط التأكيد. اطلب رابط جديد") from exc
    except BadSignature as exc:
        raise SignupError("رابط التأكيد غير صحيح") from exc
    user = session.get(User, data.get("uid"))
    if user is None or user.email != data.get("email"):
        raise SignupError("رابط التأكيد غير صحيح")
    if not user.email_verified:
        user.email_verified = True
        session.add(AuditLog(user_id=user.id, action="user.email_verified", details={"email": user.email}))
    return user


def _email_html(title: str, body: str, button: str, link: str) -> str:
    return f"""<div dir="rtl" style="font-family:Tahoma,Arial,sans-serif;max-width:480px;margin:auto;color:#1c2330">
  <h2 style="color:#0f6e5a">🔑 مفتاح الخزينة</h2>
  <h3>{escape(title)}</h3>
  <p style="line-height:1.8">{body}</p>
  <p><a href="{escape(link)}" style="display:inline-block;background:#0f6e5a;color:#fff;padding:10px 22px;border-radius:8px;text-decoration:none">{escape(button)}</a></p>
  <p style="color:#6b7482;font-size:13px">لو الزر ما اشتغل، انسخ هذا الرابط في المتصفح:<br><span dir="ltr">{escape(link)}</span></p>
</div>"""


def send_verification(user: User, base_url: str) -> bool:
    link = f"{base_url.rstrip('/')}/verify-email?token={verification_token(user)}"
    body = f"أهلًا {escape(user.full_name)}،<br>أكّد بريدك عشان تفعّل حسابك. الرابط صالح لمدة ٤٨ ساعة."
    return send_email(
        user.email,
        "أكّد بريدك — مفتاح الخزينة",
        _email_html("تأكيد البريد الإلكتروني", body, "تأكيد البريد", link),
        f"أهلًا {user.full_name}، أكّد بريدك من هذا الرابط (صالح ٤٨ ساعة):\n{link}",
    )


# ------------------------------------------------------------------ استعادة كلمة المرور


def _password_fingerprint(user: User) -> str:
    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:16]


def reset_token(user: User) -> str:
    return _reset.dumps({"uid": user.id, "fp": _password_fingerprint(user)})


def user_for_reset(session: Session, token: str) -> User:
    try:
        data = _reset.loads(token, max_age=RESET_MAX_AGE)
    except SignatureExpired as exc:
        raise SignupError("انتهت صلاحية الرابط (ساعة وحدة). اطلب رابط جديد") from exc
    except BadSignature as exc:
        raise SignupError("الرابط غير صحيح") from exc
    user = session.get(User, data.get("uid"))
    if user is None or not user.is_active or data.get("fp") != _password_fingerprint(user):
        raise SignupError("الرابط مستخدم أو غير صالح. اطلب رابط جديد")
    return user


def send_reset(user: User, base_url: str) -> bool:
    link = f"{base_url.rstrip('/')}/reset-password?token={reset_token(user)}"
    body = "وصلنا طلب لتغيير كلمة مرور حسابك. الرابط صالح لمدة ساعة ويُستخدم مرة وحدة.<br>لو ما طلبته، تجاهل الرسالة."
    return send_email(
        user.email,
        "تغيير كلمة المرور — مفتاح الخزينة",
        _email_html("تغيير كلمة المرور", body, "اختيار كلمة مرور جديدة", link),
        f"رابط تغيير كلمة المرور (صالح ساعة):\n{link}",
    )


def reset_password(session: Session, user: User, password: str, confirm: str = "") -> None:
    if confirm and password != confirm:
        raise SignupError("كلمتا المرور غير متطابقتين")
    try:
        user.password_hash = hash_password(password)
    except ValueError as exc:
        raise SignupError(str(exc)) from exc
    user.must_change_password = False
    user.email_verified = True  # وصل الرابط لبريده، فالبريد مؤكد
    session.add(AuditLog(user_id=user.id, action="user.password_reset_by_email"))
