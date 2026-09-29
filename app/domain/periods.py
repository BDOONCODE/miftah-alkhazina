"""حساب الفترات: كل بند له فترة تحددها جدولة تسويته.

- فوري: فترة واحدة دائمة (العجز المرحَّل يبقى لين يتسدد).
- يومي / أسبوعي (يبدأ الأحد) / شهري.
- يوم محدد بالشهر: الفترة تبدأ من ذاك اليوم لين اليوم اللي قبله بالشهر الجاي،
  ولو الشهر ما فيه اليوم المطلوب نعتمد آخر يوم فيه.

مفتاح الفترة نص قابل للمقارنة ترتيبيًا (يبدأ بتاريخ بداية الفترة).
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone

from .types import Frequency

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


def period_start(frequency: Frequency, on: date, settlement_day: int | None = None) -> date | None:
    if frequency is Frequency.IMMEDIATE:
        return None
    if frequency is Frequency.DAILY:
        return on
    if frequency is Frequency.WEEKLY:
        # weekday(): الاثنين=0 ... الأحد=6
        return on - timedelta(days=(on.weekday() + 1) % 7)
    if frequency is Frequency.MONTHLY:
        return on.replace(day=1)
    if frequency is Frequency.DAY_OF_MONTH:
        if not settlement_day:
            raise ValueError("جدولة «يوم محدد بالشهر» تحتاج رقم اليوم")
        this_month = _clamped(on.year, on.month, settlement_day)
        if on >= this_month:
            return this_month
        prev_year, prev_month = (on.year - 1, 12) if on.month == 1 else (on.year, on.month - 1)
        return _clamped(prev_year, prev_month, settlement_day)
    raise ValueError(f"جدولة غير معروفة: {frequency}")


def period_key(frequency: Frequency, on: date, settlement_day: int | None = None) -> str:
    start = period_start(frequency, on, settlement_day)
    return PERPETUAL_KEY if start is None else start.isoformat()
