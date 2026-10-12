"""Self-signup, email confirmation and password reset pages."""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import mailer, throttle
from ..db import get_session
from ..services import signup as svc
from ..templating import templates
from ..web import flash, redirect

router = APIRouter()
# Limit requests that send email, so the site cannot be used to flood someone's inbox
EMAIL_ACTIONS_PER_WINDOW = throttle.MAX_FAILURES


def _base_url(request: Request) -> str:
    return os.environ.get("APP_URL") or str(request.base_url)


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _email_throttled(request: Request, email: str) -> bool:
    key = f"mail:{email.strip().lower()}"
    if throttle.is_blocked(_ip(request), key):
        return True
    throttle.record_failure(_ip(request), key)  # count every message, not just failures
    return False


def _page(request: Request, name: str, context: dict, status_code: int = 200):
    return templates.TemplateResponse(request, f"auth/{name}.html", {"user": None, **context}, status_code=status_code)


@router.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request):
    return _page(request, "signup", {"form": {}, "error": None, "TYPES": svc.ACCOUNT_TYPES})


@router.post("/signup")
async def signup(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    data = svc.SignupInput(
        full_name=str(form.get("full_name", "")),
        email=str(form.get("email", "")),
        password=str(form.get("password", "")),
        confirm=str(form.get("confirm", "")),
        account_type=str(form.get("account_type", "")),
    )
    values = {"full_name": data.full_name, "email": data.email, "account_type": data.account_type}
    if _email_throttled(request, data.email):
        return _page(request, "signup", {"form": values, "error": "محاولات كثيرة. حاول مرة أخرى بعد ١٠ دقائق", "TYPES": svc.ACCOUNT_TYPES}, 429)
    try:
        user = svc.register(session, data)
    except svc.SignupError as exc:
        session.rollback()
        return _page(request, "signup", {"form": values, "error": str(exc), "TYPES": svc.ACCOUNT_TYPES}, 422)
    if not mailer.is_configured():
        # Email sending is not configured: do not leave the user waiting for a link that will not arrive; verify directly
        user.email_verified = True
        session.commit()
        request.session.clear()
        request.session["user_id"] = user.id
        flash(request, "تم إنشاء حسابك 🎉 ابدأ بتسجيل أول شركة")
        return redirect("/")
    session.commit()
    svc.send_verification(user, _base_url(request))
    request.session["pending_email"] = user.email
    return redirect("/signup/check-email")


@router.get("/signup/check-email", response_class=HTMLResponse)
def check_email(request: Request):
    return _page(request, "check_email", {"email": request.session.get("pending_email", "")})


@router.post("/verify-email/resend")
async def resend_verification(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    email = str(form.get("email", "")) or request.session.get("pending_email", "")
    request.session["pending_email"] = svc.normalize_email(email)
    if _email_throttled(request, email):
        flash(request, "طلبات كثيرة. حاول مرة أخرى بعد ١٠ دقائق", "error")
        return redirect("/signup/check-email")
    user = svc.find_by_login(session, email)
    if user is not None and user.email and not user.email_verified:
        svc.send_verification(user, _base_url(request))
    # Always the same response, so we do not reveal who is registered
    flash(request, "إذا كان البريد مسجلًا وغير مؤكد، فقد أُرسل إليه رابط تأكيد جديد")
    return redirect("/signup/check-email")


@router.get("/verify-email", response_class=HTMLResponse)
def verify_email(request: Request, token: str = "", session: Session = Depends(get_session)):
    try:
        user = svc.verify_email(session, token)
    except svc.SignupError as exc:
        return _page(request, "message", {"title": "تعذّر التأكيد", "message": str(exc), "resend": True}, 400)
    session.commit()
    request.session.clear()
    request.session["user_id"] = user.id
    flash(request, "تم تأكيد بريدك وتفعيل حسابك 🎉 ابدأ بتسجيل أول شركة")
    return redirect("/")


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_form(request: Request):
    return _page(request, "forgot", {"sent": False, "mail_ready": mailer.is_configured()})


@router.post("/forgot-password")
async def forgot(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    login = str(form.get("login", ""))
    if not _email_throttled(request, login):
        user = svc.find_by_login(session, login)
        if user is not None and user.email and user.is_active:
            svc.send_reset(user, _base_url(request))
    # Always the same response, so we do not reveal who is registered
    return _page(request, "forgot", {"sent": True})


@router.get("/invite", response_class=HTMLResponse)
def invite_form(request: Request, token: str = "", session: Session = Depends(get_session)):
    try:
        invitee = svc.user_for_invite(session, token)
    except svc.SignupError as exc:
        return _page(request, "message", {"title": "رابط الدعوة غير صالح", "message": str(exc)}, 400)
    return _page(request, "invite", {"token": token, "invitee": invitee, "error": None})


@router.post("/invite")
async def accept_invite(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    token = str(form.get("token", ""))
    try:
        invitee = svc.user_for_invite(session, token)
        svc.accept_invite(session, invitee, str(form.get("password", "")), str(form.get("full_name", "")))
    except svc.SignupError as exc:
        session.rollback()
        invitee = None
        try:
            invitee = svc.user_for_invite(session, token)
        except svc.SignupError:
            pass
        return _page(request, "invite", {"token": token, "invitee": invitee, "error": str(exc)}, 422)
    session.commit()
    flash(request, "تم تفعيل حسابك. سجّل دخولك ببريدك الإلكتروني وكلمة المرور التي اخترتها")
    return redirect("/login")


@router.get("/reset-password", response_class=HTMLResponse)
def reset_form(request: Request, token: str = "", session: Session = Depends(get_session)):
    try:
        svc.user_for_reset(session, token)
    except svc.SignupError as exc:
        return _page(request, "message", {"title": "الرابط غير صالح", "message": str(exc), "forgot": True}, 400)
    return _page(request, "reset", {"token": token, "error": None})


@router.post("/reset-password")
async def reset(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    token = str(form.get("token", ""))
    try:
        user = svc.user_for_reset(session, token)
        svc.reset_password(session, user, str(form.get("password", "")), str(form.get("confirm", "")))
    except svc.SignupError as exc:
        session.rollback()
        return _page(request, "reset", {"token": token, "error": str(exc)}, 422)
    session.commit()
    throttle.reset(_ip(request), user.username)
    flash(request, "تم تغيير كلمة المرور. سجّل دخولك بكلمة المرور الجديدة")
    return redirect("/login")
