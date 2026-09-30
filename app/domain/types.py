"""أنواع المجال الأساسية لمحرك التخصيص — بدون أي اعتماد على قاعدة البيانات.

كل المبالغ أعداد صحيحة بالهللة، وكل النسب بنقاط الأساس (1550 = 15.50%).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class CalcType(StrEnum):
    PERCENTAGE = "percentage"
    FIXED_AMOUNT = "fixed_amount"


class Frequency(StrEnum):
    IMMEDIATE = "immediate"  # بدون تاريخ استحقاق: البند ياخذ حاجته فورًا
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    DAY_OF_MONTH = "day_of_month"
    QUARTERLY = "quarterly"  # كل ٣ شهور، من تاريخ استحقاق محدد
    SEMIANNUAL = "semiannual"  # كل ٦ شهور
    ANNUAL = "annual"  # كل سنة


# المواعيد اللي تحتاج «تاريخ الاستحقاق القادم» وعدد شهور دورتها
ANCHORED_MONTHS = {Frequency.QUARTERLY: 3, Frequency.SEMIANNUAL: 6, Frequency.ANNUAL: 12}


class FundingStatus(StrEnum):
    FULLY_FUNDED = "FULLY_FUNDED"
    PARTIAL = "PARTIAL"
    UNFUNDED = "UNFUNDED"
    NOT_REQUIRED = "NOT_REQUIRED"  # بند ثابت مكتمل هدفه، أو نسبة ناتجها صفر


BASIS_POINTS_FULL = 10_000  # 100%


@dataclass(frozen=True)
class BucketSpec:
    """إعدادات بند واحد داخل نسخة سياسة."""

    bucket_id: int
    name: str
    priority: int
    calc_type: CalcType
    value: int  # نقاط أساس للنسبة، هللات للمبلغ الثابت
    frequency: Frequency
    settlement_day: int | None = None
    destination: str = ""
    protected: bool = False
    due_date: date | None = None  # تاريخ الاستحقاق القادم (للربع سنوي والنصف سنوي والسنوي)


@dataclass(frozen=True)
class BucketState:
    """الحالة التراكمية لبند في فترته الحالية."""

    bucket_id: int
    period_key: str
    target: int  # هدف الفترة للبند الثابت، وصفر للنسبة
    funded: int  # المتجمّع للبند في هذه الفترة
    carried_deficit: int  # عجز البند النسبي المرحَّل للمعاملات الجاية

    @property
    def deficit(self) -> int:
        """العجز الحالي: الباقي من الهدف للثابت، أو المرحَّل للنسبة."""
        if self.target:
            return max(0, self.target - self.funded)
        return self.carried_deficit


@dataclass(frozen=True)
class AllocationLine:
    bucket_id: int
    period_key: str
    required: int
    allocated: int
    status: FundingStatus
    state_before: BucketState | None  # None = أول مرة يتموّل فيها البند
    state_after: BucketState


@dataclass(frozen=True)
class PeriodClosure:
    """فترة انتهت لبند: كم تموّل فيها، وكم بقي عجز نهائي."""

    bucket_id: int
    period_key: str
    target: int
    funded: int
    closed_deficit: int


@dataclass(frozen=True)
class AllocationResult:
    amount: int
    lines: tuple[AllocationLine, ...]
    surplus: int
    closures: tuple[PeriodClosure, ...]

    @property
    def allocated_total(self) -> int:
        return sum(line.allocated for line in self.lines)
