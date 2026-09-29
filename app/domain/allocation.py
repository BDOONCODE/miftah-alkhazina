"""محرك التخصيص (waterfall) — دالة نقية تطبّق السياسة على معاملة واردة واحدة.

لكل بند حسب الأولوية:
    نسبة:  المطلوب = floor(النسبة × المبلغ) + العجز المرحَّل
    ثابت:  المطلوب = هدف الفترة − المتجمّع له
    المخصص = الأصغر بين المطلوب والمتبقي
الباقي بعد كل البنود = فائض.  الشرط الثابت: مجموع المخصص + الفائض = المبلغ.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from .periods import PERPETUAL_KEY, period_key
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
    """المعاملة تاريخها في فترة انتهت لبند واحد على الأقل."""


def percentage_share(amount: int, basis_points: int) -> int:
    return amount * basis_points // BASIS_POINTS_FULL


def _period_start_date(key: str) -> date | None:
    return None if key == PERPETUAL_KEY else date.fromisoformat(key)


def _roll_state(
    bucket: BucketSpec, state: BucketState | None, on: date
) -> tuple[BucketState, PeriodClosure | None]:
    """يرجّع حالة البند في فترة التاريخ `on`، ويقفل الفترة السابقة لو انتهت."""
    key = period_key(bucket.frequency, on, bucket.settlement_day)
    target = bucket.value if bucket.calc_type is CalcType.FIXED_AMOUNT else 0

    if state is None:
        return BucketState(bucket.bucket_id, key, target, 0, 0), None

    if state.period_key == key:
        # نفس الفترة: الهدف يتبع السياسة الحالية (لو تغيّرت بمنتصف الفترة)
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
    """يوزّع `amount` (بالهللة) على البنود حسب الأولوية، بدون أي أثر جانبي."""
    if amount <= 0:
        raise ValueError("مبلغ المعاملة لازم يكون أكبر من صفر")

    # نتحقق من كل البنود قبل ما نحسب أي شي، عشان الرفض يكون كامل أو لا شي
    rolled: dict[int, tuple[BucketState, PeriodClosure | None]] = {
        b.bucket_id: _roll_state(b, states.get(b.bucket_id), on) for b in buckets
    }

    remaining = amount
    lines: list[AllocationLine] = []
    for bucket in sorted(buckets, key=lambda b: b.priority):
        current, _ = rolled[bucket.bucket_id]

        if bucket.calc_type is CalcType.PERCENTAGE:
            required = percentage_share(amount, bucket.value) + current.carried_deficit
        else:
            required = max(0, current.target - current.funded)

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

        carried = required - allocated if bucket.calc_type is CalcType.PERCENTAGE else 0
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
