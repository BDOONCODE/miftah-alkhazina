from __future__ import annotations

import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 8
_ENGLISH_SYMBOL = re.compile(r"[!-/:-@\[-`{-~]")  # English keyboard symbols
_ENGLISH_ONLY = re.compile(r"[!-~]*")  # any English keyboard character, no Arabic or spaces

# Each rule: (check, message). The same rules are shown to the user while typing (password.js)
PASSWORD_RULES = [
    (lambda p: len(p) >= MIN_PASSWORD_LENGTH, f"{MIN_PASSWORD_LENGTH} أحرف على الأقل"),
    (lambda p: re.search(r"[A-Z]", p) is not None, "حرف إنجليزي كبير (A-Z)"),
    (lambda p: re.search(r"[a-z]", p) is not None, "حرف إنجليزي صغير (a-z)"),
    (lambda p: re.search(r"[0-9]", p) is not None, "رقم (0-9)"),
    (lambda p: _ENGLISH_SYMBOL.search(p) is not None, "رمز خاص مثل ! @ # $"),
    (lambda p: _ENGLISH_ONLY.fullmatch(p) is not None, "أحرف إنجليزية فقط (دون حروف عربية أو مسافات)"),
]


def password_problems(password: str) -> list[str]:
    return [message for check, message in PASSWORD_RULES if not check(password)]


def hash_password(password: str, *, enforce_rules: bool = True) -> str:
    """enforce_rules=False only for passwords from server settings (temporary, changed on first login)."""
    if enforce_rules and (problems := password_problems(password)):
        raise ValueError("يجب أن تحتوي كلمة المرور على: " + "، ".join(problems))
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"يجب أن تتكون كلمة المرور من {MIN_PASSWORD_LENGTH} أحرف على الأقل")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
