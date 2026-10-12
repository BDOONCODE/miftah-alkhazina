"""Allocation engine (waterfall): a pure function that applies the policy to one incoming transaction.

For each item, by priority:
    percentage:  required = floor(rate x amount) + carried deficit
    VAT:         required = round(amount x rate / (1 + rate)) + carried deficit   (amount already includes the tax)
    fixed:       required = due by today - funded so far
           (no due date: the whole target is due at once;
            with a due date: it accrues day by day and completes before the due date)
    allocated = min(required, remaining)
What is left after all items is surplus. Invariant: sum(allocated) + surplus == amount.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from .periods import PERPETUAL_KEY, accrued_target, period_key
from .types import (
    BASIS_POINTS_FULL,
    AllocationLine,
    AllocationResult,
    BucketSpec,
    BucketState,
    CalcType,
    FundingStatus,
    PeriodClosure,
)


class ClosedPeriodError(ValueError):
    """The transaction falls in a period that has already closed for at least one item."""


def percentage_share(amount: int, basis_points: int) -> int:
    return amount * basis_points // BASIS_POINTS_FULL


def inclusive_tax_share(amount: int, basis_points: int) -> int:
    """The tax contained in a tax-inclusive amount: 115 at 15% contains 15.

    Rounded to the nearest halala, like a tax invoice (10,000 contains 1,304.347 -> 1,304.35).
    """
    denominator = BASIS_POINTS_FULL + basis_points
    return (2 * amount * basis_points + denominator) // (2 * denominator)


def rate_share(calc_type: CalcType, amount: int, basis_points: int) -> int:
    if calc_type is CalcType.VAT_INCLUSIVE:
        return inclusive_tax_share(amount, basis_points)
    return percentage_share(amount, basis_points)


def _period_start_date(key: str) -> date | None:
    return None if key == PERPETUAL_KEY else date.fromisoformat(key)


def _roll_state(
    bucket: BucketSpec, state: BucketState | None, on: date
) -> tuple[BucketState, PeriodClosure | None]:
    """Return the item's state for the period of `on`, closing the previous period if it ended."""
    key = period_key(bucket.frequency, on, bucket.settlement_day, bucket.due_date)
    target = bucket.value if bucket.calc_type is CalcType.FIXED_AMOUNT else 0

    if state is None:
        return BucketState(bucket.bucket_id, key, target, 0, 0), None

    if state.period_key == key:
        # Same period: the target follows the current policy (in case it changed mid-period)
        return BucketState(bucket.bucket_id, key, target, state.funded, state.carried_deficit), None

    old_start = _period_start_date(state.period_key)
    if old_start is not None and on < old_start:
        raise ClosedPeriodError(
            f"تاريخ المعاملة {on.isoformat()} يقع في فترة مقفلة للبند «{bucket.name}» "
            f"(الفترة الحالية تبدأ {state.period_key})"
        )

    closure = PeriodClosure(
        bucket_id=bucket.bucket_id,
        period_key=state.period_key,
        target=state.target,
        funded=state.funded,
        closed_deficit=state.deficit,
    )
    return BucketState(bucket.bucket_id, key, target, 0, 0), closure


def allocate(
    buckets: Sequence[BucketSpec],
    states: Mapping[int, BucketState],
    amount: int,
    on: date,
) -> AllocationResult:
    """Distribute `amount` (halalas) across the items by priority, with no side effects."""
    if amount <= 0:
        raise ValueError("يجب أن يكون مبلغ المعاملة أكبر من صفر")

    # Validate every item before computing anything, so a rejection is all-or-nothing
    rolled: dict[int, tuple[BucketState, PeriodClosure | None]] = {
        b.bucket_id: _roll_state(b, states.get(b.bucket_id), on) for b in buckets
    }

    remaining = amount
    lines: list[AllocationLine] = []
    for bucket in sorted(buckets, key=lambda b: b.priority):
        current, _ = rolled[bucket.bucket_id]

        if bucket.calc_type.is_rate:
            required = rate_share(bucket.calc_type, amount, bucket.value) + current.carried_deficit
        else:
            # With a due date: take only what is due by today (gradual accrual). Without one: the whole target now
            due_by_now = accrued_target(current.target, bucket.frequency, on, bucket.settlement_day, bucket.due_date)
            required = max(0, due_by_now - current.funded)

        allocated = min(required, remaining)
        remaining -= allocated

        if required == 0:
            status = FundingStatus.NOT_REQUIRED
        elif allocated == required:
            status = FundingStatus.FULLY_FUNDED
        elif allocated > 0:
            status = FundingStatus.PARTIAL
        else:
            status = FundingStatus.UNFUNDED

        carried = required - allocated if bucket.calc_type.is_rate else 0
        after = BucketState(
            bucket.bucket_id, current.period_key, current.target, current.funded + allocated, carried
        )
        lines.append(
            AllocationLine(
                bucket_id=bucket.bucket_id,
                period_key=current.period_key,
                required=required,
                allocated=allocated,
                status=status,
                state_before=states.get(bucket.bucket_id),
                state_after=after,
            )
        )

    closures = tuple(c for _, c in rolled.values() if c is not None)
    result = AllocationResult(amount=amount, lines=tuple(lines), surplus=remaining, closures=closures)
    assert result.allocated_total + result.surplus == amount
    return result
