"""Core domain types for the allocation engine, with no database dependency.

All amounts are integers in halalas, and all rates are basis points (1550 = 15.50%).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class CalcType(StrEnum):
    PERCENTAGE = "percentage"  # a share of each incoming amount
    FIXED_AMOUNT = "fixed_amount"
    # VAT: sales amounts already include the tax, so the tax is part of the amount itself;
    # its share = amount x rate / (1 + rate). At 15%, 115 riyals contain 15 riyals of tax
    VAT_INCLUSIVE = "vat_inclusive"

    @property
    def is_rate(self) -> bool:
        """A type whose share is computed from each incoming amount (its deficit carries over), not a fixed target."""
        return self is not CalcType.FIXED_AMOUNT


class Frequency(StrEnum):
    IMMEDIATE = "immediate"  # no due date: the item takes what it needs at once
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    DAY_OF_MONTH = "day_of_month"
    QUARTERLY = "quarterly"  # every 3 months, from a set due date
    SEMIANNUAL = "semiannual"  # every 6 months
    ANNUAL = "annual"  # every year


# Schedules that need a "next due date", and their cycle length in months
ANCHORED_MONTHS = {Frequency.QUARTERLY: 3, Frequency.SEMIANNUAL: 6, Frequency.ANNUAL: 12}


class FundingStatus(StrEnum):
    FULLY_FUNDED = "FULLY_FUNDED"
    PARTIAL = "PARTIAL"
    UNFUNDED = "UNFUNDED"
    NOT_REQUIRED = "NOT_REQUIRED"  # a fixed item whose target is met, or a percentage that comes to zero


BASIS_POINTS_FULL = 10_000  # 100%


@dataclass(frozen=True)
class BucketSpec:
    """Settings for one item within a policy version."""

    bucket_id: int
    name: str
    priority: int
    calc_type: CalcType
    value: int  # basis points for rates, halalas for fixed amounts
    frequency: Frequency
    settlement_day: int | None = None
    destination: str = ""
    protected: bool = False
    due_date: date | None = None  # next due date (quarterly, semiannual and annual)


@dataclass(frozen=True)
class BucketState:
    """Cumulative state of an item in its current period."""

    bucket_id: int
    period_key: str
    target: int  # period target for a fixed item, zero for rates
    funded: int  # funded for the item in this period
    carried_deficit: int  # deficit of a rate item carried to the next transactions

    @property
    def deficit(self) -> int:
        """Current deficit: what is left of the target for fixed items, or the carried amount for rates."""
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
    state_before: BucketState | None  # None = first time the item is funded
    state_after: BucketState


@dataclass(frozen=True)
class PeriodClosure:
    """A period that ended for an item: how much it was funded and the final deficit."""

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
