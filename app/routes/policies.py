from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..auth import current_user, get_entity_for, require_role
from ..db import get_session
from ..domain.money import format_amount, parse_amount, parse_percent
from ..domain.policy_rules import validate_policy
from ..domain.types import BASIS_POINTS_FULL, CalcType, Frequency
from ..models import Policy, PolicyStatus, Role, User
from ..services import policies as svc
from ..templating import templates
from ..web import flash, redirect

router = APIRouter()
editor = require_role(Role.ACCOUNTANT, Role.ADMIN)
decider = require_role(Role.APPROVER, Role.ADMIN)


def _policy_for(user: User, policy_id: int, session: Session) -> Policy:
    policy = session.get(Policy, policy_id)
    if policy is None:
        raise HTTPException(404, "السياسة غير موجودة")
    get_entity_for(user, policy.entity_id, session)
    return policy


def _row_view(pb) -> dict:
    """البند كنصوص للنموذج."""
    value = str(pb.value / 100).rstrip("0").rstrip(".") if pb.calc_type is CalcType.PERCENTAGE else format_amount(pb.value)
    return {
        "bucket_id": str(pb.bucket_id),
        "name": pb.name,
        "priority": str(pb.priority),
        "calc_type": pb.calc_type.value,
        "value": value,
        "frequency": pb.frequency.value,
        "settlement_day": str(pb.settlement_day or ""),
        "destination": pb.destination,
        "protected": "1" if pb.protected else "0",
    }


def _parse_rows(form) -> tuple[list[dict], list[svc.BucketInput], list[str]]:
    fields = ["bucket_id", "name", "priority", "calc_type", "value", "frequency", "settlement_day", "destination", "protected"]
    columns = {f: form.getlist(f) for f in fields}
    count = len(columns["name"])
    if any(len(v) != count for v in columns.values()):
        raise HTTPException(400, "نموذج غير مكتمل")

    views, inputs, errors = [], [], []
    for i in range(count):
        row = {f: columns[f][i] for f in fields}
        views.append(row)
        label = f"السطر {i + 1}"
        try:
            calc = CalcType(row["calc_type"])
            freq = Frequency(row["frequency"])
            value = parse_percent(row["value"]) if calc is CalcType.PERCENTAGE else parse_amount(row["value"])
            priority = int(row["priority"])
            day = int(row["settlement_day"]) if row["settlement_day"].strip() else None
        except ValueError as exc:
            errors.append(f"{label}: {exc}" if str(exc) else f"{label}: قيمة غير صالحة")
            continue
        inputs.append(
            svc.BucketInput(
                bucket_id=int(row["bucket_id"]) if row["bucket_id"] else None,
                name=row["name"],
                priority=priority,
                calc_type=calc,
                value=value,
                frequency=freq,
                settlement_day=day,
                destination=row["destination"],
                protected=row["protected"] == "1",
            )
        )
    return views, inputs, errors


@router.get("/entities/{entity_id}/policy", response_class=HTMLResponse)
def policy_overview(
    request: Request, entity_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    entity = get_entity_for(user, entity_id, session)
    open_ = svc.open_policy(session, entity_id)
    return templates.TemplateResponse(
        request,
        "policy/overview.html",
        {
            "user": user,
            "entity": entity,
            "tab": "policy",
            "active": svc.active_policy(session, entity_id),
            "open": open_,
            "open_changes": svc.protected_changes_for(session, open_) if open_ else [],
            "history": svc.history(session, entity_id),
        },
    )


@router.post("/entities/{entity_id}/policy/draft")
def new_draft(request: Request, entity_id: int, user: User = Depends(editor), session: Session = Depends(get_session)):
    entity = get_entity_for(user, entity_id, session)
    try:
        draft = svc.start_draft(session, entity, user)
    except svc.PolicyError as exc:
        flash(request, str(exc), "error")
        return redirect(f"/entities/{entity_id}/policy")
    session.commit()
    return redirect(f"/policies/{draft.id}/edit")


def _render_editor(request, user, policy, session, rows, errors, notes, status_code=200):
    return templates.TemplateResponse(
        request,
        "policy/edit.html",
        {
            "user": user,
            "entity": get_entity_for(user, policy.entity_id, session),
            "tab": "policy",
            "policy": policy,
            "rows": rows,
            "errors": errors,
            "notes": notes,
            "protected_changes": svc.protected_changes_for(session, policy),
            "pct_total": sum(pb.value for pb in policy.buckets if pb.calc_type is CalcType.PERCENTAGE)
            / (BASIS_POINTS_FULL / 100),
        },
        status_code=status_code,
    )


@router.get("/policies/{policy_id}/edit", response_class=HTMLResponse)
def edit_draft(request: Request, policy_id: int, user: User = Depends(editor), session: Session = Depends(get_session)):
    policy = _policy_for(user, policy_id, session)
    if policy.status is not PolicyStatus.DRAFT:
        return redirect(f"/policies/{policy.id}")
    rows = [_row_view(pb) for pb in policy.buckets]
    return _render_editor(request, user, policy, session, rows, validate_policy(svc.specs(policy)), policy.notes)


@router.post("/policies/{policy_id}/edit")
async def save_draft(request: Request, policy_id: int, user: User = Depends(editor), session: Session = Depends(get_session)):
    policy = _policy_for(user, policy_id, session)
    form = await request.form()
    views, inputs, errors = _parse_rows(form)
    notes = str(form.get("notes", ""))
    if errors:
        return _render_editor(request, user, policy, session, views, errors, notes, status_code=422)
    try:
        svc.save_draft(session, policy, inputs, notes, user)
    except svc.PolicyError as exc:
        return _render_editor(request, user, policy, session, views, [str(exc)], notes, status_code=422)
    session.commit()

    if form.get("intent") == "submit":
        try:
            svc.submit(session, policy, user, str(form.get("justification", "")))
        except svc.PolicyError as exc:
            session.rollback()
            # أخطاء التحقق تظهر أصلًا فوق الجدول، فما نكررها في التنبيه
            detail = "راجع الملاحظات تحت" if validate_policy(svc.specs(policy)) else str(exc)
            flash(request, f"انحفظت المسودة لكن ما انرسلت للمراجع: {detail}", "error")
            return redirect(f"/policies/{policy.id}/edit")
        session.commit()
        flash(request, "انرسلت السياسة للمراجع. بتصير نشطة أول ما يعتمدها")
        return redirect(f"/entities/{policy.entity_id}/policy")

    if validate_policy(svc.specs(policy)):
        flash(request, "انحفظت المسودة، لكن فيها ملاحظات لازم تنحل قبل الإرسال للاعتماد", "warning")
    else:
        flash(request, "انحفظت المسودة")
    return redirect(f"/policies/{policy.id}/edit")


@router.get("/policies/{policy_id}", response_class=HTMLResponse)
def view_policy(request: Request, policy_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    policy = _policy_for(user, policy_id, session)
    return templates.TemplateResponse(
        request,
        "policy/view.html",
        {
            "user": user,
            "entity": get_entity_for(user, policy.entity_id, session),
            "tab": "policy",
            "policy": policy,
            "protected_changes": svc.protected_changes_for(session, policy)
            if policy.status in svc.OPEN_STATUSES
            else [],
        },
    )


def _action(request: Request, policy_id: int, user: User, session: Session, fn, success: str, **kwargs):
    policy = _policy_for(user, policy_id, session)
    try:
        fn(session, policy, user, **kwargs)
    except svc.PolicyError as exc:
        session.rollback()
        flash(request, str(exc), "error")
        return redirect(f"/policies/{policy_id}" if policy.status is not PolicyStatus.DRAFT else f"/policies/{policy_id}/edit")
    session.commit()
    flash(request, success)
    return redirect(f"/entities/{policy.entity_id}/policy")


@router.post("/policies/{policy_id}/approve")
async def approve(request: Request, policy_id: int, user: User = Depends(decider), session: Session = Depends(get_session)):
    form = await request.form()
    return _action(
        request, policy_id, user, session, svc.approve, "اعتُمدت السياسة وصارت نشطة",
        note=str(form.get("note", "")),
    )


@router.post("/policies/{policy_id}/reject")
async def reject(request: Request, policy_id: int, user: User = Depends(decider), session: Session = Depends(get_session)):
    form = await request.form()
    return _action(
        request, policy_id, user, session, svc.reject, "رُفضت السياسة", note=str(form.get("note", ""))
    )


@router.post("/policies/{policy_id}/discard")
def discard(request: Request, policy_id: int, user: User = Depends(editor), session: Session = Depends(get_session)):
    return _action(request, policy_id, user, session, svc.discard_draft, "انحذفت المسودة")
