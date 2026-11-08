"""Encryption of client data at rest (AES-256-GCM), field by field.

Every value is encrypted with a fresh random nonce, so the same IBAN never looks the same twice in the
database. Columns that must be looked up (IBANs) also keep a blind index: a keyed HMAC of the normalized
value, which can be compared for equality but not reversed.

The key comes from DATA_ENCRYPTION_KEY (any long random string). It is required in production; locally it is
generated once into .data_key. Losing the key means losing the encrypted data, so keep a copy of it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import BASE_DIR, IS_PRODUCTION

PREFIX = "enc1:"
_NONCE_BYTES = 12


def _raw_key() -> bytes:
    if key := os.environ.get("DATA_ENCRYPTION_KEY", "").strip():
        return key.encode()
    if IS_PRODUCTION:
        raise RuntimeError("يجب تحديد DATA_ENCRYPTION_KEY في متغيرات البيئة للإنتاج")
    key_file = BASE_DIR / ".data_key"
    if not key_file.exists():
        key_file.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    return key_file.read_text(encoding="utf-8").strip().encode()


def _derive(raw: bytes, purpose: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=purpose).derive(raw)


_RAW = _raw_key()
_AEAD = AESGCM(_derive(_RAW, b"miftah/field-encryption/v1"))
_INDEX_KEY = _derive(_RAW, b"miftah/blind-index/v1")


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(PREFIX)


def encrypt(value: str | None) -> str | None:
    if value is None or value == "" or is_encrypted(value):
        return value
    nonce = os.urandom(_NONCE_BYTES)
    sealed = _AEAD.encrypt(nonce, value.encode("utf-8"), None)
    return PREFIX + base64.urlsafe_b64encode(nonce + sealed).decode("ascii")


def decrypt(value: str | None) -> str | None:
    """Values written before encryption was switched on are returned as they are."""
    if not is_encrypted(value):
        return value
    blob = base64.urlsafe_b64decode(value[len(PREFIX):])
    return _AEAD.decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], None).decode("utf-8")


def encrypt_json(value) -> str | None:
    return None if value is None else encrypt(json.dumps(value, ensure_ascii=False))


def decrypt_json(value: str | None):
    if value is None or value == "":
        return None if value is None else {}
    if isinstance(value, (dict, list)):  # a driver that already parsed legacy JSON
        return value
    return json.loads(decrypt(value))


def iban_index(iban: str | None) -> str | None:
    """Blind index of an IBAN, for lookups without decrypting every row."""
    if not iban:
        return None
    normalized = re.sub(r"\s+", "", iban).upper()
    return hmac.new(_INDEX_KEY, normalized.encode("ascii", "ignore"), hashlib.sha256).hexdigest()
