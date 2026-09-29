"""الإعدادات من متغيرات البيئة، بقيم افتراضية مناسبة للتطوير المحلي."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent



def _database_url() -> str:
    """SQLite محليًا. للإنتاج رابط PostgreSQL (مثلًا Supabase) كما هو من لوحتهم."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return f"sqlite:///{(BASE_DIR / 'treasury.db').as_posix()}"
    # Supabase وغيره يعطون postgres:// أو postgresql://؛ نستخدم مشغّل psycopg 3
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


DATABASE_URL = _database_url()
IS_PRODUCTION = not DATABASE_URL.startswith("sqlite")


def _secret_key() -> str:
    if key := os.environ.get("SECRET_KEY"):
        return key
    if IS_PRODUCTION:
        raise RuntimeError("SECRET_KEY لازم يتحدد في متغيرات البيئة للإنتاج")
    key_file = BASE_DIR / ".secret_key"
    if not key_file.exists():
        key_file.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    return key_file.read_text(encoding="utf-8").strip()


SECRET_KEY = _secret_key()
HTTPS_ONLY_COOKIES = os.environ.get("HTTPS_ONLY_COOKIES", "0") == "1"
