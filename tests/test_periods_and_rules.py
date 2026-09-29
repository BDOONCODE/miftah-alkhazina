from datetime import date, datetime, timezone

import pytest

from app.domain.money import format_amount, parse_amount, parse_percent
from app.domain.periods import PERPETUAL_KEY, local_date, period_key
from app.domain.policy_rules import justification_error, protected_changes, validate_policy
from app.domain.types import BucketSpec, CalcType, Frequency

F = Frequency


@pytest.mark.parametrize(
    "freq, on, day, expected",
    [
        (F.IMMEDIATE, date(2026, 9, 29), None, PERPETUAL_KEY),
        (F.DAILY, date(2026, 9, 29), None, "2026-09-29"),
        (F.WEEKLY, date(2026, 9, 29), None, "2026-09-27"),  # الثلاثاء ← الأحد
        (F.WEEKLY, date(2026, 9, 27), None, "2026-09-27"),  # الأحد نفسه
        (F.WEEKLY, date(2026, 9, 26), None, "2026-09-20"),  # السبت آخر الأسبوع
        (F.MONTHLY, date(2026, 9, 29), None, "2026-09-01"),
        (F.DAY_OF_MONTH, date(2026, 9, 29), 25, "2026-09-25"),
        (F.DAY_OF_MONTH, date(2026, 9, 10), 25, "2026-08-25"),
        (F.DAY_OF_MONTH, date(2026, 2, 28), 31, "2026-02-28"),  # فبراير: آخر يوم
        (F.DAY_OF_MONTH, date(2026, 2, 27), 31, "2026-01-31"),
        (F.DAY_OF_MONTH, date(2026, 1, 5), 10, "2025-12-10"),  # عبور السنة
    ],
)
def test_period_key(freq, on, day, expected):
    assert period_key(freq, on, day) == expected


def test_local_date_uses_riyadh_time():
    # 22:30 UTC = 01:30 الرياض من اليوم التالي
    assert local_date(datetime(2026, 9, 29, 22, 30, tzinfo=timezone.utc)) == date(2026, 9, 30)


def test_money_parsing():
    assert parse_amount("1,234.56") == 123456
    assert parse_amount("١٢٣٤٫٥") == 123450
    assert parse_percent("15.5") == 1550
    assert format_amount(123456) == "1,234.56"
    with pytest.raises(ValueError):
        parse_amount("1.234")


def b(i, priority, calc=CalcType.PERCENTAGE, value=1000, freq=F.IMMEDIATE, **kw):
    return BucketSpec(i, f"بند{i}", priority, calc, value, freq, **kw)


def test_validate_policy_errors():
    assert validate_policy([b(1, 1), b(2, 2, CalcType.FIXED_AMOUNT, 500, F.MONTHLY)]) == []
    assert "أولوية" in validate_policy([b(1, 1), b(2, 1)])[0]
    assert any("100%" in e for e in validate_policy([b(1, 1, value=6000), b(2, 2, value=5000)]))
    assert any("فوري" in e for e in validate_policy([b(1, 1, CalcType.FIXED_AMOUNT, 500, F.IMMEDIATE)]))
    assert any("يوم" in e for e in validate_policy([b(1, 1, freq=F.DAY_OF_MONTH)]))
    assert validate_policy([])


def test_protected_changes_need_justification():
    old = [b(1, 1, protected=True), b(2, 2)]
    changed = [b(1, 1, value=900, protected=True), b(2, 3)]
    changes = protected_changes(old, changed, funded_bucket_ids={1, 2})
    assert changes == ["تغيير قيمة البند المحمي «بند1»"]  # البند 2 غير محمي
    assert justification_error(changes, "قصير")
    assert justification_error(changes, "رفع الضريبة بسبب تعديل نظامي من الهيئة") is None
    # بند محمي ما تموّل في الفترة الحالية: التعديل حر
    assert protected_changes(old, changed, funded_bucket_ids=set()) == []
    assert protected_changes(old, [b(2, 2)], funded_bucket_ids={1}) == ["حذف البند المحمي «بند1»"]
