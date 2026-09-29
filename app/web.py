"""أدوات مشتركة للصفحات: رسائل التنبيه وإعادة التوجيه."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse


def flash(request: Request, message: str, kind: str = "success") -> None:
    request.session.setdefault("flashes", []).append({"message": message, "kind": kind})


def pop_flashes(request: Request) -> list[dict]:
    return request.session.pop("flashes", [])


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)
