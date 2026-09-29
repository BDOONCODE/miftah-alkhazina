from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from ..auth import current_user, get_entity_for, require_role
from ..db import get_session
from ..domain import formula
from ..models import CustomMetric, DashboardWidget, Role, User
from ..services import dashboards as svc
from ..services import metrics as m
from ..templating import templates
from ..web import flash, redirect

router = APIRouter()
editor = require_role(Role.ACCOUNTANT, Role.ADMIN)


@router.get("/entities/{entity_id}/dashboard", response_class=HTMLResponse)
def dashboard_page(
    request: Request, entity_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)
):
    entity = get_entity_for(user, entity_id, session)
    dashboard = svc.get_or_create(session, entity, user)
    session.commit()
    customs = svc.customs_for(session, entity_id)
    snap = m.snapshot(session, entity_id, m.resolve_range("this_month"))
    values = m.variables(snap)
    return templates.TemplateResponse(
        request,
        "dashboard/page.html",
        {
            "user": user,
            "entity": entity,
            "tab": "dashboard",
            "dashboard": dashboard,
            "views": svc.widget_views(session, dashboard),
            "can_edit": user.role in (Role.ACCOUNTANT, Role.ADMIN),
            "catalog": m.metric_catalog(snap, customs),
            "customs": [
                (c, m.format_value(_safe_eval(c.formula, values), c.fmt)) for c in customs
            ],
            "variable_names": list(values),
            "KINDS": svc.WIDGET_KINDS,
            "PERIODS": m.PERIOD_LABELS,
            "CHARTS": svc.CHARTS,
            "GRANULARITIES": svc.GRANULARITIES,
            "FORMATS": svc.FORMATS,
        },
    )


def _safe_eval(expr: str, values: dict) -> float | None:
    try:
        return formula.evaluate(expr, values)
    except formula.FormulaError:
        return None


def _widget_for(user: User, widget_id: int, session: Session) -> DashboardWidget:
    widget = session.get(DashboardWidget, widget_id)
    if widget is None:
        raise HTTPException(404, "العنصر غير موجود")
    get_entity_for(user, widget.dashboard.entity_id, session)
    return widget


@router.post("/entities/{entity_id}/dashboard/widgets")
async def add_widget(request: Request, entity_id: int, user: User = Depends(editor), session: Session = Depends(get_session)):
    entity = get_entity_for(user, entity_id, session)
    form = dict(await request.form())
    dashboard = svc.get_or_create(session, entity, user)
    try:
        svc.add_widget(session, dashboard, user, str(form.get("kind", "")), str(form.get("title", "")), form)
    except svc.DashboardError as exc:
        session.rollback()
        flash(request, str(exc), "error")
        return redirect(f"/entities/{entity_id}/dashboard")
    session.commit()
    flash(request, "انضاف العنصر للوحة")
    return redirect(f"/entities/{entity_id}/dashboard")


@router.post("/dashboard/widgets/{widget_id}")
async def update_widget(request: Request, widget_id: int, user: User = Depends(editor), session: Session = Depends(get_session)):
    widget = _widget_for(user, widget_id, session)
    form = dict(await request.form())
    try:
        svc.update_widget(session, widget, user, str(form.get("title", "")), form)
    except svc.DashboardError as exc:
        session.rollback()
        flash(request, str(exc), "error")
    else:
        session.commit()
    return redirect(f"/entities/{widget.dashboard.entity_id}/dashboard")


@router.post("/dashboard/widgets/{widget_id}/delete")
def delete_widget(request: Request, widget_id: int, user: User = Depends(editor), session: Session = Depends(get_session)):
    widget = _widget_for(user, widget_id, session)
    entity_id = widget.dashboard.entity_id
    svc.delete_widget(session, widget, user)
    session.commit()
    return redirect(f"/entities/{entity_id}/dashboard")


@router.post("/entities/{entity_id}/dashboard/layout")
async def save_layout(request: Request, entity_id: int, user: User = Depends(editor), session: Session = Depends(get_session)):
    entity = get_entity_for(user, entity_id, session)
    try:
        items = (await request.json())["items"]
        svc.save_layout(session, svc.get_or_create(session, entity, user), user, items)
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "بيانات الترتيب غير صالحة")
    session.commit()
    return JSONResponse({"ok": True})


# ------------------------------------------------------------------ المؤشرات المخصصة


@router.post("/entities/{entity_id}/metrics")
async def save_metric(request: Request, entity_id: int, user: User = Depends(editor), session: Session = Depends(get_session)):
    get_entity_for(user, entity_id, session)
    form = await request.form()
    metric = None
    if metric_id := str(form.get("metric_id", "")):
        metric = session.get(CustomMetric, int(metric_id))
        if metric is None or metric.entity_id != entity_id:
            raise HTTPException(404, "المؤشر غير موجود")
    try:
        saved = svc.save_custom_metric(
            session, entity_id, user, str(form.get("name", "")), str(form.get("formula", "")), str(form.get("fmt", "number")), metric
        )
    except svc.DashboardError as exc:
        session.rollback()
        flash(request, f"ما انحفظ المؤشر: {exc}", "error")
        return redirect(f"/entities/{entity_id}/dashboard#metrics")
    session.commit()
    flash(request, f"انحفظ المؤشر «{saved.name}». تقدر تضيفه للوحة كبطاقة رقم")
    return redirect(f"/entities/{entity_id}/dashboard#metrics")


@router.post("/metrics/{metric_id}/delete")
def delete_metric(request: Request, metric_id: int, user: User = Depends(editor), session: Session = Depends(get_session)):
    metric = session.get(CustomMetric, metric_id)
    if metric is None:
        raise HTTPException(404, "المؤشر غير موجود")
    get_entity_for(user, metric.entity_id, session)
    entity_id = metric.entity_id
    svc.delete_custom_metric(session, metric, user)
    session.commit()
    flash(request, "انحذف المؤشر والعناصر اللي تعرضه")
    return redirect(f"/entities/{entity_id}/dashboard#metrics")
