"""Periods: each item has a cycle set by its schedule, and the cycle ends on the due date.

- Immediate: no due date, one perpetual period (the item takes what it needs at once; a carried deficit stays until paid).
- Daily / weekly (starts Sunday) / monthly.
- Day of month: the cycle runs from that day to the same day next month;
  if a month lacks that day, its last day is used.
- Quarterly / semiannual / annual: cycles are anchored on the "next due date" set by the accountant,
  every 3, 6 or 12 months before and after it.

The period key is a sortable string (the cycle start date).
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone

from .types import ANCHORED_MONTHS, Frequency

RIYADH = timezone(timedelta(hours=3), "Asia/Riyadh")  # no daylight saving time
PERPETUAL_KEY = "0000-00-00"


def local_date(moment: datetime | date) -> date:
    """The date of the moment in Riyadh time."""
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
    """(cycle start, cycle end = due date). Immediate = (None, None)."""
    if frequency is Frequency.IMMEDIATE:
        return None, None
    if frequency is Frequency.DAILY:
        return on, on + timedelta(days=1)
    if frequency is Frequency.WEEKLY:
        start = on - timedelta(days=(on.weekday() + 1) % 7)  # weekday(): Monday=0 ... Sunday=6
        return start, start + timedelta(days=7)
    if frequency is Frequency.MONTHLY:
        start = on.replace(day=1)
        return start, _add_months(start, 1, 1)
    if frequency is Frequency.DAY_OF_MONTH:
        if not settlement_day:
            raise ValueError("جدولة «يوم محدد من الشهر» تتطلب رقم اليوم")
        start = _clamped(on.year, on.month, settlement_day)
        if on < start:
            start = _add_months(on.replace(day=1), -1, settlement_day)
        return start, _add_months(start.replace(day=1), 1, settlement_day)
    if frequency in ANCHORED_MONTHS:
        if due_date is None:
            raise ValueError("تتطلب هذه الجدولة تاريخ الاستحقاق القادم")
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
    """How much a fixed-amount item should have accrued by day `on` (inclusive).

    No due date (immediate): the whole target at once.
    With a due date: accrues day by day and completes on the last day before the due date.
    """
    start, end = period_bounds(frequency, on, settlement_day, due_date)
    if start is None:
        return target
    total = (end - start).days
    elapsed = min(total, (on - start).days + 1)
    return -(-target * elapsed // total)  # round up so the target completes exactly on the last day
