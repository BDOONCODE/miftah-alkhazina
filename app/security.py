from __future__ import annotations

import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 8
_ENGLISH_SYMBOL = re.compile(r"[!-/:-@\[-`{-~]")  # رموز لوحة المفاتيح الإنجليزية
_ENGLISH_ONLY = re.compile(r"[!-~]*")  # كل حرف من لوحة المفاتيح الإنجليزية، بدون عربي أو مسافات

# كل شرط: (فحص، رسالة). نفس الشروط تنعرض للمستخدم وهو يكتب (password.js)
PASSWORD_RULES = [
    (lambda p: len(p) >= MIN_PASSWORD_LENGTH, f"{MIN_PASSWORD_LENGTH} أحرف على الأقل"),
    (lambda p: re.search(r"[A-Z]", p) is not None, "حرف إنجليزي كبير (A-Z)"),
    (lambda p: re.search(r"[a-z]", p) is not None, "حرف إنجليزي صغير (a-z)"),
    (lambda p: re.search(r"[0-9]", p) is not None, "رقم (0-9)"),
    (lambda p: _ENGLISH_SYMBOL.search(p) is not None, "رمز خاص مثل ! @ # $"),
    (lambda p: _ENGLISH_ONLY.fullmatch(p) is not None, "حروف وأرقام ورموز إنجليزية فقط (بدون عربي أو مسافات)"),
]


def password_problems(password: str) -> list[str]:
    return [message for check, message in PASSWORD_RULES if not check(password)]


def hash_password(password: str, *, enforce_rules: bool = True) -> str:
    """enforce_rules=False بس للكلمات اللي تجي من إعدادات الخادم (مؤقتة وتتغيّر أول دخول)."""
    if enforce_rules and (problems := password_problems(password)):
        raise ValueError("كلمة المرور لازم تحتوي: " + "، ".join(problems))
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"كلمة المرور لازم تكون {MIN_PASSWORD_LENGTH} أحرف على الأقل")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
