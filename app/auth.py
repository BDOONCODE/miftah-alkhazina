"""Sessions and permissions: who the current user is, and whether they may do this."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_session
from .models import Entity, Role, User


class LoginRequired(Exception):
    """Turned into a redirect to the login page."""


class PasswordChangeRequired(Exception):
    """Temporary password: must be changed before anything else."""


def current_user_any(request: Request, session: Session = Depends(get_session)) -> User:
    """The signed-in user, even with a temporary password (for the change-password page only)."""
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
            raise HTTPException(status.HTTP_403_FORBIDDEN, "ليست لديك صلاحية لهذا الإجراء")
        return user

    return checker


def accessible_entities(user: User, session: Session) -> list[Entity]:
    if user.role is Role.ADMIN:
        return list(session.scalars(select(Entity).order_by(Entity.name)))
    return sorted(user.entities, key=lambda e: e.name)


def get_entity_for(user: User, entity_id: int, session: Session, *, allow_reviewer: bool = False) -> Entity:
    """Return the entity if the user may access it, otherwise 404 (we do not reveal that it exists).

    Reviewers only review policies: every company page refuses them unless it opts in with allow_reviewer
    (only the policy pages do), so a new page is closed to reviewers by default."""
    entity = session.get(Entity, entity_id)
    if entity is None or (user.role is not Role.ADMIN and entity not in user.entities):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "الشركة غير موجودة")
    if user.role is Role.APPROVER and not allow_reviewer:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "دور المراجع مقتصر على مراجعة سياسة التقسيم واعتمادها")
    return entity
