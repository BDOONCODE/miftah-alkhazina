"""الجلسات والصلاحيات: من المستخدم الحالي، وهل يحق له هذا الإجراء."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_session
from .models import Entity, Role, User


class LoginRequired(Exception):
    """تُحوَّل إلى إعادة توجيه لصفحة الدخول."""


class PasswordChangeRequired(Exception):
    """كلمة مرور مؤقتة: لازم تتغيّر قبل أي شي ثاني."""


def current_user_any(request: Request, session: Session = Depends(get_session)) -> User:
    """المستخدم المسجّل، حتى لو كلمة مروره مؤقتة (لصفحة تغيير كلمة المرور فقط)."""
    user_id = request.session.get("user_id")
    user = session.get(User, user_id) if user_id else None
    if user is None or not user.is_active:
        request.session.clear()
        raise LoginRequired
    return user


def current_user(user: User = Depends(current_user_any)) -> User:
    if user.must_change_password:
        raise PasswordChangeRequired
    return user


def require_role(*roles: Role):
    def checker(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "ما عندك صلاحية لهذا الإجراء")
        return user

    return checker


def accessible_entities(user: User, session: Session) -> list[Entity]:
    if user.role is Role.ADMIN:
        return list(session.scalars(select(Entity).order_by(Entity.name)))
    return sorted(user.entities, key=lambda e: e.name)


def get_entity_for(user: User, entity_id: int, session: Session) -> Entity:
    """يرجّع الكيان لو المستخدم مسموح له فيه، وإلا 404 (ما نكشف وجوده)."""
    entity = session.get(Entity, entity_id)
    if entity is None or (user.role is not Role.ADMIN and entity not in user.entities):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "الشركة غير موجودة")
    return entity
