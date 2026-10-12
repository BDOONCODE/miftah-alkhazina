from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..auth import current_user, get_entity_for, require_role
from ..db import get_session
from ..domain.money import format_amount, parse_amount, parse_percent
from ..domain.policy_rules import effective_rate, validate_policy
from ..domain.types import BASIS_POINTS_FULL, CalcType, Frequency
from ..models import Policy, PolicyStatus, Role, User
from ..services import policies as svc
from ..services.accounts import DESTINATION_KINDS, INCOME_KINDS, accounts_for
from ..templating import templates
from ..web import flash, redirect

router = APIRouter()
editor = require_role(Role.ACCOUNTANT, Role.ADMIN)


def _policy_for(user: User, policy_id: int, session: Session) -> Policy:
    policy = session.get(Policy, policy_id)
    if policy is None:
        raise HTTPException(404, "السياسة غير موجودة")
    get_entity_for(user, policy.entity_id, session, allow_reviewer=True)
    return policy


def _row_view(pb) -> dict:
    """The item as form strings."""
    value = str(pb.value / 100).rstrip("0").rstrip(".") if pb.calc_type.is_rate else format_amount(pb.value)
    return {
        "bucket_id": str(pb.bucket_id),
        "name": pb.name,
        "priority": str(pb.priority),
        "calc_type": pb.calc_type.value,
        "value": value,
        "frequency": pb.frequency.value,
        "settlement_day": str(pb.settlement_day or ""),
        "destination": str(pb.destination_account_id or ""),
        # Legacy destination stored as text (before bank accounts): shown as a hint so the account can be picked
        "legacy_destination": "" if pb.destination_account_id else pb.destination,
        "protected": "1" if pb.protected else "0",
        "dest_iban": "",
        "dest_kind": "external",
        "due_date": pb.due_date.isoformat() if pb.due_date else "",
    }


def _parse_rows(form) -> tuple[list[dict], list[svc.BucketInput], list[str]]:
    fields = ["bucket_id", "name", "priority", "calc_type", "value", "frequency", "settlement_day", "destination", "protected"]
    # Fields for "+ new IBAN" and the due date
    optional = {"dest_iban": "", "dest_kind": "external", "due_date": ""}
    columns = {f: form.getlist(f) for f in fields}
    count = len(columns["name"])
    if any(len(v) != count for v in columns.values()):
        raise HTTPException(400, "نموذج غير مكتمل")
    for f, default in optional.items():
        values = form.getlist(f)
        columns[f] = values if len(values) == count else [default] * count
    fields += list(optional)

    views, inputs, errors = [], [], []
    for i in range(count):
        row = {f: columns[f][i] for f in fields}
        views.append(row)
        label = f"السطر {i + 1}"
        try:
            calc = CalcType(row["calc_type"])
            freq = Frequency(row["frequency"])
            value = parse_percent(row["value"]) if calc.is_rate else parse_amount(row["value"])
            priority = int(row["priority"])
            day = int(row["settlement_day"]) if row["settlement_day"].strip() else None
            due = date.fromisoformat(row["due_date"]) if row["due_date"].strip() else None
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
                destination="",
                destination_account_id=int(row["destination"]) if row["destination"].isdigit() else None,
                new_destination_iban=row["dest_iban"] if row["destination"] == "new" else "",
                new_destination_kind=row["dest_kind"],
                due_date=due,
                protected=row["protected"] == "1",
            )
        )
    return views, inputs, errors


@router.get("/entities/{entity_id}/policy", response_class=HTMLResponse)
def policy_overview(
    request: Request, entity_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    entity = get_entity_for(user, entity_id, session, allow_reviewer=True)
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
            "can_decide": bool(open_) and svc.can_decide(session, open_, user),
            "history": svc.history(session, entity_id),
        },
    )


@router.post("/entities/{entity_id}/policy/draft")
def new_draft(request: Request, entity_id: int, user: User = Depends(editor), session: Session = Depends(get_session)):
    entity = get_entity_for(user, entity_id, session, allow_reviewer=True)
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
            "entity": get_entity_for(user, policy.entity_id, session, allow_reviewer=True),
            "tab": "policy",
            "policy": policy,
            "rows": rows,
            "errors": errors,
            "notes": notes,
            "surplus_account": str(policy.surplus_account_id or ""),
            "surplus_frequency": (policy.surplus_frequency or Frequency.MONTHLY).value,
            "surplus_day": str(policy.surplus_settlement_day or ""),
            "surplus_due": policy.surplus_due_date.isoformat() if policy.surplus_due_date else "",
            "SURPLUS_FREQUENCIES": [f.value for f in svc.SURPLUS_FREQUENCIES],
            "protected_changes": svc.protected_changes_for(session, policy),
            "self_approval": svc.is_self_approval(session, policy),
            "destinations": [
                a for a in accounts_for(session, policy.entity_id, active_only=True) if a.kind in DESTINATION_KINDS
            ],
            "has_income": any(a.kind in INCOME_KINDS for a in accounts_for(session, policy.entity_id, active_only=True)),
            "pct_total": sum(effective_rate(b) for b in svc.specs(policy) if b.calc_type.is_rate)
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
    return _render_editor(
        request, user, policy, session, rows, validate_policy(svc.specs(policy)) + svc.destination_errors(policy), policy.notes
    )


@router.post("/policies/{policy_id}/edit")
async def save_draft(request: Request, policy_id: int, user: User = Depends(editor), session: Session = Depends(get_session)):
    policy = _policy_for(user, policy_id, session)
    form = await request.form()
    views, inputs, errors = _parse_rows(form)
    notes = str(form.get("notes", ""))
    surplus = str(form.get("surplus_account", ""))
    surplus_id = int(surplus) if surplus.isdigit() else None
    try:
        surplus_frequency = Frequency(str(form.get("surplus_frequency", "monthly")))
        day_raw, due_raw = str(form.get("surplus_day", "")).strip(), str(form.get("surplus_due", "")).strip()
        surplus_day = int(day_raw) if day_raw else None
        surplus_due = date.fromisoformat(due_raw) if due_raw else None
    except ValueError:
        errors = [*errors, "موعد عزل الفائض غير صالح"]
        surplus_frequency, surplus_day, surplus_due = Frequency.MONTHLY, None, None
    if errors:
        return _render_editor(request, user, policy, session, views, errors, notes, status_code=422)
    try:
        svc.save_draft(session, policy, inputs, notes, user, surplus_account_id=surplus_id,
                       surplus_frequency=surplus_frequency, surplus_day=surplus_day, surplus_due=surplus_due)
    except svc.PolicyError as exc:
        return _render_editor(request, user, policy, session, views, [str(exc)], notes, status_code=422)
    session.commit()

    if form.get("intent") == "submit":
        self_approval = svc.is_self_approval(session, policy)
        try:
            svc.submit(session, policy, user, str(form.get("justification", "")))
            if self_approval:
                svc.approve(session, policy, user)
        except svc.PolicyError as exc:
            session.rollback()
            # Validation errors already show above the table, so do not repeat them in the alert
            detail = "راجع الملاحظات أدناه" if validate_policy(svc.specs(policy)) or svc.destination_errors(policy) else str(exc)
            flash(request, f"تم حفظ المسودة لكنها لم {'تُعتمد' if self_approval else 'تُرسل إلى المراجع'}: {detail}", "error")
            return redirect(f"/policies/{policy.id}/edit")
        session.commit()
        if self_approval:
            flash(request, "اعتُمدت السياسة وأصبحت نشطة. سيُقسَّم أي مبلغ وارد وفقها من الآن")
        else:
            flash(request, "أُرسلت السياسة إلى المراجع، وستصبح نشطة فور اعتمادها")
        return redirect(f"/entities/{policy.entity_id}/policy")

    if validate_policy(svc.specs(policy)) or svc.destination_errors(policy):
        flash(request, "تم حفظ المسودة، لكن فيها ملاحظات يجب معالجتها قبل إرسالها للاعتماد", "warning")
    else:
        flash(request, "تم حفظ المسودة")
    return redirect(f"/policies/{policy.id}/edit")


@router.get("/policies/{policy_id}", response_class=HTMLResponse)
def view_policy(request: Request, policy_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    policy = _policy_for(user, policy_id, session)
    return templates.TemplateResponse(
        request,
        "policy/view.html",
        {
            "user": user,
            "entity": get_entity_for(user, policy.entity_id, session, allow_reviewer=True),
            "tab": "policy",
            "policy": policy,
            "protected_changes": svc.protected_changes_for(session, policy)
            if policy.status in svc.OPEN_STATUSES
            else [],
            "can_decide": svc.can_decide(session, policy, user),
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
async def approve(request: Request, policy_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    form = await request.form()
    return _action(
        request, policy_id, user, session, svc.approve, "اعتُمدت السياسة وأصبحت نشطة",
        note=str(form.get("note", "")),
    )


@router.post("/policies/{policy_id}/reject")
async def reject(request: Request, policy_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    form = await request.form()
    return _action(
        request, policy_id, user, session, svc.reject, "رُفضت السياسة", note=str(form.get("note", ""))
    )


@router.post("/policies/{policy_id}/withdraw")
def withdraw(request: Request, policy_id: int, user: User = Depends(editor), session: Session = Depends(get_session)):
    policy = _policy_for(user, policy_id, session)
    try:
        svc.withdraw(session, policy, user)
    except svc.PolicyError as exc:
        session.rollback()
        flash(request, str(exc), "error")
        return redirect(f"/entities/{policy.entity_id}/policy")
    session.commit()
    flash(request, "أُعيدت السياسة إلى مسودة. عدّلها ثم أرسلها مرة أخرى إلى المراجع")
    return redirect(f"/policies/{policy.id}/edit")


@router.post("/policies/{policy_id}/discard")
def discard(request: Request, policy_id: int, user: User = Depends(editor), session: Session = Depends(get_session)):
    return _action(request, policy_id, user, session, svc.discard_draft, "تم حذف المسودة")
