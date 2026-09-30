"""طبقة المؤشرات: كل رقم في اللوحة والتقارير يُحسب من هنا، عشان الأرقام تتطابق في كل مكان.

المؤشرات نوعين:
- خلال فترة (الوارد، المخصص، الفائض، مخصص كل بند…) من المعاملات اللي تاريخها داخل الفترة.
- حيّة (المتجمّع، العجز الحالي) من حالة البنود في فتراتها الحالية.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain import formula
from ..domain.money import format_amount
from ..domain.periods import RIYADH, accrued_target, local_date, period_key, period_start
from ..domain.types import CalcType, Frequency
from ..models import (
    AllocationEvent,
    BucketPeriodState,
    CustomMetric,
    PeriodClosure,
    PolicyBucket,
    Transaction,
    utcnow,
)
from .policies import active_policy

PERIOD_LABELS = {
    "this_week": "هذا الأسبوع",
    "this_month": "هذا الشهر",
    "last_month": "الشهر السابق",
    "this_year": "هذه السنة",
    "last_30": "آخر ٣٠ يوم",
    "all": "من البداية",
}


@dataclass(frozen=True)
class DateRange:
    start: date | None  # None = من البداية
    end: date  # غير شامل
    label: str

    def bounds(self) -> tuple[datetime | None, datetime]:
        to_dt = lambda d: datetime.combine(d, time(), tzinfo=RIYADH)  # noqa: E731
        return (to_dt(self.start) if self.start else None, to_dt(self.end))


def today_riyadh() -> date:
    return local_date(utcnow())


def _add_month(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m + 1, 1)


def resolve_range(key: str, today: date | None = None) -> DateRange:
    today = today or today_riyadh()
    tomorrow = today + timedelta(days=1)
    label = PERIOD_LABELS.get(key, PERIOD_LABELS["this_month"])
    if key == "this_week":
        return DateRange(period_start(Frequency.WEEKLY, today), tomorrow, label)
    if key == "last_month":
        start = _add_month(today.replace(day=1), -1)
        return DateRange(start, today.replace(day=1), label)
    if key == "this_year":
        return DateRange(date(today.year, 1, 1), tomorrow, label)
    if key == "last_30":
        return DateRange(today - timedelta(days=29), tomorrow, label)
    if key == "all":
        return DateRange(None, tomorrow, label)
    return DateRange(today.replace(day=1), tomorrow, PERIOD_LABELS["this_month"])


REPORT_KINDS = {"week": "أسبوعي", "month": "شهري", "year": "سنوي"}


def report_range(kind: str, anchor: date) -> DateRange:
    """الأسبوع (من الأحد) أو الشهر أو السنة اللي فيها التاريخ."""
    if kind == "week":
        start = period_start(Frequency.WEEKLY, anchor)
        end = start + timedelta(days=7)
        return DateRange(start, end, f"أسبوع {start.isoformat()} إلى {(end - timedelta(days=1)).isoformat()}")
    if kind == "year":
        return DateRange(date(anchor.year, 1, 1), date(anchor.year + 1, 1, 1), f"سنة {anchor.year}")
    start = anchor.replace(day=1)
    return DateRange(start, _add_month(start, 1), f"شهر {start:%Y-%m}")


def var_slug(name: str) -> str:
    slug = re.sub(r"\W+", "_", name).strip("_") or "بند"
    return slug if slug[0].isalpha() or slug[0] == "_" else f"_{slug}"


@dataclass
class BucketView:
    bucket_id: int
    name: str
    priority: int
    calc_type: CalcType | None
    value: int
    frequency: Frequency | None
    allocated: int = 0  # خلال الفترة
    target: int = 0  # حي
    funded: int = 0  # حي
    deficit: int = 0  # حي
    in_policy: bool = True

    @property
    def progress(self) -> float | None:
        return min(1.0, self.funded / self.target) if self.target else None


@dataclass
class Snapshot:
    range: DateRange
    incoming: int = 0
    surplus: int = 0
    count: int = 0
    closed_deficit: int = 0
    buckets: list[BucketView] = field(default_factory=list)
    transactions: list[Transaction] = field(default_factory=list)

    @property
    def allocated(self) -> int:
        return self.incoming - self.surplus

    @property
    def deficit(self) -> int:
        return sum(b.deficit for b in self.buckets)


def _range_filter(stmt, rng: DateRange):
    start, end = rng.bounds()
    stmt = stmt.where(Transaction.occurred_at < end)
    return stmt.where(Transaction.occurred_at >= start) if start else stmt


def snapshot(session: Session, entity_id: int, rng: DateRange, today: date | None = None) -> Snapshot:
    today = today or today_riyadh()
    snap = Snapshot(range=rng)

    txns = list(
        session.scalars(_range_filter(select(Transaction).where(Transaction.entity_id == entity_id), rng).order_by(Transaction.occurred_at, Transaction.id))
    )
    snap.transactions = txns
    snap.incoming = sum(t.amount for t in txns)
    snap.surplus = sum(t.surplus for t in txns)
    snap.count = sum(1 for t in txns if t.reversal_of is None) - sum(1 for t in txns if t.reversal_of is not None)

    allocated: dict[int, int] = defaultdict(int)
    if txns:
        for event in session.scalars(select(AllocationEvent).where(AllocationEvent.transaction_id.in_([t.id for t in txns]))):
            allocated[event.bucket_id] += event.allocated

    views: dict[int, BucketView] = {}
    policy = active_policy(session, entity_id)
    states = {
        s.bucket_id: s for s in session.scalars(select(BucketPeriodState).where(BucketPeriodState.entity_id == entity_id))
    }
    for pb in policy.buckets if policy else []:
        view = BucketView(pb.bucket_id, pb.name, pb.priority, pb.calc_type, pb.value, pb.frequency)
        state = states.get(pb.bucket_id)
        current = state is not None and state.period_key == period_key(
            pb.frequency, today, pb.settlement_day, pb.due_date
        )
        if pb.calc_type is CalcType.FIXED_AMOUNT:
            view.target = pb.value
            view.funded = state.funded if current else 0
            # العجز = المتأخر عن الجدول لين اليوم، مو باقي الهدف كامل (الباقي مو مستحق للحين)
            due_by_today = accrued_target(pb.value, pb.frequency, today, pb.settlement_day, pb.due_date)
            view.deficit = max(0, due_by_today - view.funded)
        else:
            view.funded = state.funded if current else 0
            view.deficit = state.carried_deficit if current else 0
        views[pb.bucket_id] = view

    missing = [b for b in allocated if b not in views]
    if missing:  # بنود انحذفت من السياسة لكن لها تخصيص خلال الفترة
        for pb in session.scalars(select(PolicyBucket).where(PolicyBucket.bucket_id.in_(missing)).order_by(PolicyBucket.id.desc())):
            views.setdefault(
                pb.bucket_id, BucketView(pb.bucket_id, pb.name, 1000 + pb.priority, pb.calc_type, pb.value, pb.frequency, in_policy=False)
            )
    for bucket_id, amount in allocated.items():
        views[bucket_id].allocated = amount
    snap.buckets = sorted(views.values(), key=lambda v: v.priority)

    start, end = rng.start, rng.end
    closures = session.scalars(
        select(PeriodClosure).where(PeriodClosure.entity_id == entity_id, PeriodClosure.voided.is_(False))
    )
    snap.closed_deficit = sum(
        c.closed_deficit
        for c in closures
        if c.period_key == "0000-00-00" or ((start is None or date.fromisoformat(c.period_key) >= start) and date.fromisoformat(c.period_key) < end)
    )
    return snap


# ------------------------------------------------------------------ المتغيرات والمعادلات

BASE_METRICS = {
    "incoming": ("الوارد", "sar"),
    "allocated": ("المخصص", "sar"),
    "surplus": ("الفائض", "sar"),
    "deficit": ("العجز", "sar"),
    "closed_deficit": ("العجز_المقفل", "sar"),
    "count": ("عدد_المعاملات", "number"),
}
BASE_LABELS = {
    "incoming": "إجمالي الوارد",
    "allocated": "المخصص للبنود",
    "surplus": "الفائض",
    "deficit": "العجز الحالي",
    "closed_deficit": "عجز الفترات المقفلة",
    "count": "عدد المعاملات",
}
BUCKET_METRICS = {
    "bucket_allocated": ("مخصص", "المخصص لـ"),
    "bucket_funded": ("متجمع", "المتجمّع لـ"),
    "bucket_target": ("هدف", "هدف "),
    "bucket_deficit": ("عجز", "عجز "),
}


def _base_value(snap: Snapshot, key: str) -> int:
    return getattr(snap, key)


def _bucket_value(view: BucketView, kind: str) -> int:
    return {"bucket_allocated": view.allocated, "bucket_funded": view.funded, "bucket_target": view.target, "bucket_deficit": view.deficit}[kind]


def variables(snap: Snapshot) -> dict[str, float]:
    """قيم المتغيرات للمعادلات بالريال (عدد المعاملات كرقم)."""
    out: dict[str, float] = {}
    for key, (var, fmt) in BASE_METRICS.items():
        value = _base_value(snap, key)
        out[var] = value / 100 if fmt == "sar" else value
    for view in snap.buckets:
        for kind, (prefix, _) in BUCKET_METRICS.items():
            out[f"{prefix}_{var_slug(view.name)}"] = _bucket_value(view, kind) / 100
    return out


def variable_names(snap: Snapshot) -> list[str]:
    return list(variables(snap).keys())


def format_value(value: float | int | None, fmt: str) -> str:
    if value is None:
        return "—"
    if fmt == "percent":
        return f"{value:,.1f}%"
    if fmt == "sar":
        return f"{format_amount(round(value * 100))} ر.س"
    return f"{value:,.2f}".rstrip("0").rstrip(".") if isinstance(value, float) else f"{value:,}"


def metric_catalog(snap: Snapshot, customs: list[CustomMetric]) -> list[tuple[str, str]]:
    """خيارات المؤشر في إعداد العنصر: (المفتاح، الاسم)."""
    options = [(k, label) for k, label in BASE_LABELS.items()]
    for view in snap.buckets:
        for kind, (_, label) in BUCKET_METRICS.items():
            options.append((f"{kind}:{view.bucket_id}", f"{label}{view.name}"))
    options += [(f"custom:{c.id}", f"⚙ {c.name}") for c in customs]
    return options


def metric_value(snap: Snapshot, key: str, customs: dict[int, CustomMetric]) -> tuple[str, float | None, str]:
    """(الاسم، القيمة بالريال أو كرقم، التنسيق)."""
    if key in BASE_METRICS:
        _, fmt = BASE_METRICS[key]
        raw = _base_value(snap, key)
        return BASE_LABELS[key], raw / 100 if fmt == "sar" else raw, fmt
    kind, _, ident = key.partition(":")
    if kind in BUCKET_METRICS and ident.isdigit():
        view = next((b for b in snap.buckets if b.bucket_id == int(ident)), None)
        if view is None:
            return "بند غير موجود", None, "sar"
        return f"{BUCKET_METRICS[kind][1]}{view.name}", _bucket_value(view, kind) / 100, "sar"
    if kind == "custom" and ident.isdigit() and int(ident) in customs:
        metric = customs[int(ident)]
        try:
            return metric.name, formula.evaluate(metric.formula, variables(snap)), metric.fmt
        except formula.FormulaError:
            return metric.name, None, metric.fmt  # مثلًا بند انحذف من السياسة
    return "مؤشر غير معروف", None, "number"


def timeseries(snap: Snapshot, granularity: str) -> dict:
    """الوارد والمخصص والفائض مجمّعة يوميًا أو أسبوعيًا أو شهريًا."""
    def bucket_of(d: date) -> date:
        if granularity == "week":
            return period_start(Frequency.WEEKLY, d)
        if granularity == "month":
            return d.replace(day=1)
        return d

    incoming: dict[date, int] = defaultdict(int)
    surplus: dict[date, int] = defaultdict(int)
    for t in snap.transactions:
        key = bucket_of(local_date(t.occurred_at))
        incoming[key] += t.amount
        surplus[key] += t.surplus
    keys = sorted(incoming)
    fmt = {"month": "%Y-%m", "week": "%m-%d", "day": "%m-%d"}.get(granularity, "%m-%d")
    return {
        "labels": [k.strftime(fmt) for k in keys],
        "series": [
            {"name": "الوارد", "data": [incoming[k] / 100 for k in keys]},
            {"name": "المخصص", "data": [(incoming[k] - surplus[k]) / 100 for k in keys]},
            {"name": "الفائض", "data": [surplus[k] / 100 for k in keys]},
        ],
    }
