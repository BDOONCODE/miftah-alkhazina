"""حساب المستخدم (تغيير كلمة المرور) وإدارة المحاسبين للمدير."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import current_user_any, require_role
from ..db import get_session
from ..models import AuditLog, Role, User
from ..security import hash_password, verify_password
from ..services.companies import CompanyError, create_user
from ..templating import templates
from ..web import flash, redirect

router = APIRouter()
admin_only = require_role(Role.ADMIN)


@router.get("/account/password", response_class=HTMLResponse)
def password_form(request: Request, user: User = Depends(current_user_any)):
    return templates.TemplateResponse(request, "account/password.html", {"user": user, "error": None})


@router.post("/account/password")
async def change_password(
    request: Request, user: User = Depends(current_user_any), session: Session = Depends(get_session)
):
    form = await request.form()
    current, new, confirm = (str(form.get(k, "")) for k in ("current", "new", "confirm"))
    error = None
    if not verify_password(user.password_hash, current):
        error = "كلمة المرور الحالية غير صحيحة"
    elif confirm and new != confirm:
        error = "كلمتا المرور الجديدتان غير متطابقتين"
    elif new == current:
        error = "اختر كلمة مرور مختلفة عن الحالية"
    else:
        try:
            user.password_hash = hash_password(new)
        except ValueError as exc:
            error = str(exc)
    if error:
        return templates.TemplateResponse(request, "account/password.html", {"user": user, "error": error}, status_code=422)
    user.must_change_password = False
    session.add(AuditLog(user_id=user.id, action="user.password_changed"))
    session.commit()
    flash(request, "تغيّرت كلمة المرور")
    return redirect("/")


@router.get("/admin/users", response_class=HTMLResponse)
def users_page(request: Request, user: User = Depends(admin_only), session: Session = Depends(get_session)):
    users = session.scalars(select(User).order_by(User.role, User.full_name)).all()
    return templates.TemplateResponse(request, "account/users.html", {"user": user, "users": users, "error": None})


@router.post("/admin/users")
async def create_accountant(request: Request, user: User = Depends(admin_only), session: Session = Depends(get_session)):
    form = await request.form()
    try:
        created = create_user(
            session,
            full_name=str(form.get("full_name", "")),
            username=str(form.get("username", "")),
            password=str(form.get("password", "")),
            role=Role.ACCOUNTANT,
            actor=user,
        )
    except CompanyError as exc:
        session.rollback()
        flash(request, str(exc), "error")
        return redirect("/admin/users")
    session.commit()
    flash(request, f"أُنشئ حساب المحاسب «{created.full_name}»")
    return redirect("/admin/users")


@router.post("/admin/users/{user_id}/reset-password")
async def reset_password(
    request: Request, user_id: int, user: User = Depends(admin_only), session: Session = Depends(get_session)
):
    """المدير يعطي المستخدم كلمة مرور مؤقتة جديدة، والنظام يطلب تغييرها أول دخول."""
    target = session.get(User, user_id)
    if target is None:
        flash(request, "المستخدم غير موجود", "error")
        return redirect("/admin/users")
    form = await request.form()
    try:
        target.password_hash = hash_password(str(form.get("password", "")))
    except ValueError as exc:
        flash(request, str(exc), "error")
        return redirect("/admin/users")
    target.must_change_password = True
    target.is_active = True
    session.add(AuditLog(user_id=user.id, action="user.password_reset", details={"username": target.username}))
    session.commit()
    flash(request, f"تغيّرت كلمة مرور «{target.username}». أعطه الكلمة المؤقتة، وبيطلب منه النظام يغيّرها أول دخول")
    return redirect("/admin/users")


@router.post("/admin/users/{user_id}/verify-email")
def verify_email_manually(
    request: Request, user_id: int, user: User = Depends(admin_only), session: Session = Depends(get_session)
):
    """لو رسالة التأكيد ما وصلت، المدير يفعّل البريد يدويًا."""
    target = session.get(User, user_id)
    if target is None:
        flash(request, "المستخدم غير موجود", "error")
        return redirect("/admin/users")
    target.email_verified = True
    session.add(AuditLog(user_id=user.id, action="user.email_verified_by_admin", details={"username": target.username}))
    session.commit()
    flash(request, f"تفعّل حساب «{target.username}». يقدر يدخل الحين")
    return redirect("/admin/users")


@router.post("/admin/users/{user_id}/toggle")
def toggle_user(request: Request, user_id: int, user: User = Depends(admin_only), session: Session = Depends(get_session)):
    target = session.get(User, user_id)
    if target is None or target.id == user.id:
        flash(request, "ما يمكن تعطيل هذا الحساب", "error")
        return redirect("/admin/users")
    target.is_active = not target.is_active
    session.add(
        AuditLog(user_id=user.id, action="user.toggled", details={"username": target.username, "active": target.is_active})
    )
    session.commit()
    return redirect("/admin/users")
