"""اللوحة القابلة للتخصيص: عناصر يضيفها المحاسب ويرتّبها، ومؤشرات بمعادلات خاصة بالشركة."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain import formula
from ..models import AuditLog, CustomMetric, Dashboard, DashboardWidget, Entity, User
from . import metrics as m

WIDGET_KINDS = {
    "kpi": "بطاقة رقم",
    "bucket_progress": "تقدّم البنود",
    "allocation": "توزيع التخصيص",
    "timeseries": "الحركة الزمنية",
    "deficits": "قائمة العجز",
    "recent": "آخر المعاملات",
}
DEFAULT_SIZES = {"kpi": (3, 2), "bucket_progress": (6, 4), "allocation": (6, 4), "timeseries": (12, 4), "deficits": (6, 3), "recent": (12, 4)}
CHARTS = {"pie": "دائري", "bar": "أعمدة"}
GRANULARITIES = {"day": "يومي", "week": "أسبوعي", "month": "شهري"}
FORMATS = {"sar": "ريال", "percent": "نسبة %", "number": "رقم"}

DEFAULT_LAYOUT = [
    ("kpi", "الوارد هذا الشهر", {"metric": "incoming", "period": "this_month"}, 0, 0),
    ("kpi", "الفائض", {"metric": "surplus", "period": "this_month"}, 3, 0),
    ("kpi", "العجز الحالي", {"metric": "deficit", "period": "this_month"}, 6, 0),
    ("kpi", "عدد المعاملات", {"metric": "count", "period": "this_month"}, 9, 0),
    ("bucket_progress", "تقدّم البنود", {"period": "this_month"}, 0, 2),
    ("allocation", "توزيع الوارد", {"period": "this_month", "chart": "pie"}, 6, 2),
]


class DashboardError(ValueError):
    pass


def get_or_create(session: Session, entity: Entity, user: User) -> Dashboard:
    dashboard = session.scalar(select(Dashboard).where(Dashboard.entity_id == entity.id))
    if dashboard is None:
        dashboard = Dashboard(entity_id=entity.id, title=f"لوحة {entity.name}", updated_by=user.id)
        session.add(dashboard)
        session.flush()
    if not dashboard.widgets and not dashboard.initialized:
        apply_default_layout(dashboard)
    return dashboard


def apply_default_layout(dashboard: Dashboard) -> None:
    for kind, title, config, x, y in DEFAULT_LAYOUT:
        w, h = DEFAULT_SIZES[kind]
        dashboard.widgets.append(DashboardWidget(kind=kind, title=title, config=config, x=x, y=y, w=w, h=h))
    dashboard.initialized = True


def clean_config(kind: str, form: dict) -> dict:
    if kind not in WIDGET_KINDS:
        raise DashboardError("نوع عنصر غير معروف")
    config: dict = {"period": form.get("period") if form.get("period") in m.PERIOD_LABELS else "this_month"}
    if kind == "kpi":
        metric = str(form.get("metric", ""))
        if not metric:
            raise DashboardError("اختر المؤشر")
        config["metric"] = metric
    if kind == "allocation":
        config["chart"] = form.get("chart") if form.get("chart") in CHARTS else "pie"
    if kind == "timeseries":
        config["granularity"] = form.get("granularity") if form.get("granularity") in GRANULARITIES else "day"
    if kind == "recent":
        try:
            config["limit"] = max(1, min(50, int(form.get("limit") or 10)))
        except ValueError:
            config["limit"] = 10
    return config


def _audit(session: Session, user: User, entity_id: int, action: str, **details) -> None:
    session.add(AuditLog(user_id=user.id, entity_id=entity_id, action=action, details=details))


def add_widget(session: Session, dashboard: Dashboard, user: User, kind: str, title: str, form: dict) -> DashboardWidget:
    config = clean_config(kind, form)
    w, h = DEFAULT_SIZES[kind]
    bottom = max((wd.y + wd.h for wd in dashboard.widgets), default=0)
    widget = DashboardWidget(kind=kind, title=title.strip() or WIDGET_KINDS[kind], config=config, x=0, y=bottom, w=w, h=h)
    dashboard.widgets.append(widget)
    dashboard.updated_by = user.id
    session.flush()
    _audit(session, user, dashboard.entity_id, "dashboard.widget_added", widget_id=widget.id, kind=kind)
    return widget


def update_widget(session: Session, widget: DashboardWidget, user: User, title: str, form: dict) -> None:
    widget.config = clean_config(widget.kind, form)
    widget.title = title.strip() or WIDGET_KINDS[widget.kind]
    widget.dashboard.updated_by = user.id
    _audit(session, user, widget.dashboard.entity_id, "dashboard.widget_updated", widget_id=widget.id)


def delete_widget(session: Session, widget: DashboardWidget, user: User) -> None:
    _audit(session, user, widget.dashboard.entity_id, "dashboard.widget_deleted", widget_id=widget.id, kind=widget.kind)
    session.delete(widget)


def save_layout(session: Session, dashboard: Dashboard, user: User, items: list[dict]) -> None:
    by_id = {w.id: w for w in dashboard.widgets}
    for item in items:
        widget = by_id.get(int(item.get("id", 0)))
        if widget is None:
            continue
        widget.x, widget.y = max(0, int(item["x"])), max(0, int(item["y"]))
        widget.w, widget.h = max(1, min(12, int(item["w"]))), max(1, min(12, int(item["h"])))
    dashboard.updated_by = user.id


# ------------------------------------------------------------------ المؤشرات المخصصة


def customs_for(session: Session, entity_id: int) -> list[CustomMetric]:
    return list(session.scalars(select(CustomMetric).where(CustomMetric.entity_id == entity_id).order_by(CustomMetric.name)))


def save_custom_metric(
    session: Session, entity_id: int, user: User, name: str, expr: str, fmt: str, metric: CustomMetric | None = None
) -> CustomMetric:
    name = name.strip()
    if not name:
        raise DashboardError("اسم المؤشر مطلوب")
    if fmt not in FORMATS:
        raise DashboardError("تنسيق غير معروف")
    duplicate = session.scalar(select(CustomMetric).where(CustomMetric.entity_id == entity_id, CustomMetric.name == name))
    if duplicate is not None and duplicate is not metric:
        raise DashboardError(f"في مؤشر بنفس الاسم «{name}»")
    allowed = m.variable_names(m.snapshot(session, entity_id, m.resolve_range("this_month")))
    try:
        formula.validate(expr, allowed)
    except formula.FormulaError as exc:
        raise DashboardError(str(exc)) from exc
    if metric is None:
        metric = CustomMetric(entity_id=entity_id, created_by=user.id, name=name, formula=expr.strip(), fmt=fmt)
        session.add(metric)
    else:
        metric.name, metric.formula, metric.fmt = name, expr.strip(), fmt
    session.flush()
    _audit(session, user, entity_id, "metric.saved", metric_id=metric.id, name=name, formula=expr.strip())
    return metric


def delete_custom_metric(session: Session, metric: CustomMetric, user: User) -> None:
    key = f"custom:{metric.id}"
    dashboard = session.scalar(select(Dashboard).where(Dashboard.entity_id == metric.entity_id))
    for widget in list(dashboard.widgets if dashboard else []):
        if widget.config.get("metric") == key:
            session.delete(widget)
    _audit(session, user, metric.entity_id, "metric.deleted", metric_id=metric.id, name=metric.name)
    session.delete(metric)


# ------------------------------------------------------------------ بيانات العرض


def widget_views(session: Session, dashboard: Dashboard) -> list[dict]:
    customs = {c.id: c for c in customs_for(session, dashboard.entity_id)}
    snaps: dict[str, m.Snapshot] = {}

    def snap_for(period: str) -> m.Snapshot:
        if period not in snaps:
            snaps[period] = m.snapshot(session, dashboard.entity_id, m.resolve_range(period))
        return snaps[period]

    views = []
    for widget in dashboard.widgets:
        cfg = widget.config or {}
        snap = snap_for(cfg.get("period", "this_month"))
        view = {"widget": widget, "period_label": snap.range.label, "kind": widget.kind}
        if widget.kind == "kpi":
            label, value, fmt = m.metric_value(snap, cfg.get("metric", "incoming"), customs)
            view.update(label=label, display=m.format_value(value, fmt), negative=value is not None and value < 0)
        elif widget.kind == "bucket_progress":
            view["buckets"] = [b for b in snap.buckets if b.in_policy]
        elif widget.kind == "allocation":
            data = [{"name": b.name, "value": b.allocated / 100} for b in snap.buckets if b.allocated]
            if snap.surplus:
                data.append({"name": "الفائض", "value": snap.surplus / 100})
            view["chart"] = {"type": cfg.get("chart", "pie"), "data": data}
        elif widget.kind == "timeseries":
            view["chart"] = {"type": "line", **m.timeseries(snap, cfg.get("granularity", "day"))}
        elif widget.kind == "deficits":
            view["rows"] = sorted((b for b in snap.buckets if b.deficit > 0), key=lambda b: -b.deficit)
        elif widget.kind == "recent":
            view["rows"] = list(reversed(snap.transactions))[: cfg.get("limit", 10)]
        views.append(view)
    return views
