"""حساب الفترات: كل بند له دورة تحددها جدولة استحقاقه، ونهاية الدورة هي تاريخ الاستحقاق.

- فوري: بدون تاريخ استحقاق، فترة واحدة دائمة (البند ياخذ حاجته فورًا، والعجز المرحَّل يبقى لين يتسدد).
- يومي / أسبوعي (يبدأ الأحد) / شهري.
- يوم محدد بالشهر: الدورة من ذاك اليوم لين نفس اليوم بالشهر الجاي،
  ولو الشهر ما فيه اليوم المطلوب نعتمد آخر يوم فيه.
- ربع سنوي / نصف سنوي / سنوي: الدورات تمشي على «تاريخ الاستحقاق القادم» اللي يحدده المحاسب،
  كل ٣ أو ٦ أو ١٢ شهر قبله وبعده.

مفتاح الفترة نص قابل للمقارنة ترتيبيًا (تاريخ بداية الدورة).
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone

from .types import ANCHORED_MONTHS, Frequency

RIYADH = timezone(timedelta(hours=3), "Asia/Riyadh")  # بدون توقيت صيفي
PERPETUAL_KEY = "0000-00-00"


def local_date(moment: datetime | date) -> date:
    """تاريخ اللحظة بتوقيت الرياض."""
    if isinstance(moment, datetime):
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=RIYADH)
        return moment.astimezone(RIYADH).date()
    return moment


def _clamped(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _add_months(d: date, months: int, day: int) -> date:
    years, month0 = divmod(d.month - 1 + months, 12)
    return _clamped(d.year + years, month0 + 1, day)


def period_bounds(
    frequency: Frequency, on: date, settlement_day: int | None = None, due_date: date | None = None
) -> tuple[date | None, date | None]:
    """(بداية الدورة، نهايتها = تاريخ الاستحقاق). فوري = (None, None)."""
    if frequency is Frequency.IMMEDIATE:
        return None, None
    if frequency is Frequency.DAILY:
        return on, on + timedelta(days=1)
    if frequency is Frequency.WEEKLY:
        start = on - timedelta(days=(on.weekday() + 1) % 7)  # weekday(): الاثنين=0 ... الأحد=6
        return start, start + timedelta(days=7)
    if frequency is Frequency.MONTHLY:
        start = on.replace(day=1)
        return start, _add_months(start, 1, 1)
    if frequency is Frequency.DAY_OF_MONTH:
        if not settlement_day:
            raise ValueError("جدولة «يوم محدد بالشهر» تحتاج رقم اليوم")
        start = _clamped(on.year, on.month, settlement_day)
        if on < start:
            start = _add_months(on.replace(day=1), -1, settlement_day)
        return start, _add_months(start.replace(day=1), 1, settlement_day)
    if frequency in ANCHORED_MONTHS:
        if due_date is None:
            raise ValueError("هذي الجدولة تحتاج تاريخ الاستحقاق القادم")
        step = ANCHORED_MONTHS[frequency]
        months = (on.year - due_date.year) * 12 + (on.month - due_date.month)
        k = months // step
        start = _add_months(due_date, k * step, due_date.day)
        if start > on:
            k -= 1
            start = _add_months(due_date, k * step, due_date.day)
        end = _add_months(due_date, (k + 1) * step, due_date.day)
        return start, end
    raise ValueError(f"جدولة غير معروفة: {frequency}")


def period_start(
    frequency: Frequency, on: date, settlement_day: int | None = None, due_date: date | None = None
) -> date | None:
    return period_bounds(frequency, on, settlement_day, due_date)[0]


def period_key(
    frequency: Frequency, on: date, settlement_day: int | None = None, due_date: date | None = None
) -> str:
    start = period_start(frequency, on, settlement_day, due_date)
    return PERPETUAL_KEY if start is None else start.isoformat()


def accrued_target(
    target: int, frequency: Frequency, on: date, settlement_day: int | None = None, due_date: date | None = None
) -> int:
    """كم لازم يكون متجمّع لبند مبلغ ثابت إلى يوم `on` (شامل).

    بدون تاريخ استحقاق (فوري): الهدف كامل فورًا.
    مع تاريخ استحقاق: يتجمّع بالتدريج بالأيام، ويكتمل في آخر يوم قبل الاستحقاق.
    """
    start, end = period_bounds(frequency, on, settlement_day, due_date)
    if start is None:
        return target
    total = (end - start).days
    elapsed = min(total, (on - start).days + 1)
    return -(-target * elapsed // total)  # تقريب للأعلى، عشان يكتمل الهدف بالضبط في آخر يوم
