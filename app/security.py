from __future__ import annotations

import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 8
# كل شرط: (نمط، رسالة). نفس القائمة تنعرض للمستخدم وهو يكتب (password_rules.js)
PASSWORD_RULES = [
    (re.compile(rf".{{{MIN_PASSWORD_LENGTH},}}", re.S), f"{MIN_PASSWORD_LENGTH} أحرف على الأقل"),
    (re.compile(r"[A-Z]"), "حرف إنجليزي كبير (A-Z)"),
    (re.compile(r"[a-z]"), "حرف إنجليزي صغير (a-z)"),
    (re.compile(r"\d"), "رقم (0-9)"),
    (re.compile(r"[^A-Za-z0-9\s]"), "رمز خاص مثل ! @ # $"),
]


def password_problems(password: str) -> list[str]:
    return [message for pattern, message in PASSWORD_RULES if not pattern.search(password)]


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
