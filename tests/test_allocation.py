from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.allocation import ClosedPeriodError, allocate
from app.domain.types import BucketSpec, CalcType, Frequency, FundingStatus

SAR = 100  # ريال بالهللات

TAX = BucketSpec(1, "الضريبة", 1, CalcType.PERCENTAGE, 1500, Frequency.IMMEDIATE, protected=True)
RENT = BucketSpec(2, "الإيجار", 2, CalcType.FIXED_AMOUNT, 10_000 * SAR, Frequency.MONTHLY)
PROFIT = BucketSpec(3, "الأرباح", 3, CalcType.PERCENTAGE, 500, Frequency.MONTHLY)
POLICY = [TAX, RENT, PROFIT]


def run(transactions, buckets=POLICY):
    """يطبّق سلسلة معاملات ويرجّع النتائج والحالة النهائية."""
    states, results = {}, []
    for amount, on in transactions:
        result = allocate(buckets, states, amount, on)
        states = {line.bucket_id: line.state_after for line in result.lines}
        results.append(result)
    return results, states


def line(result, bucket_id):
    return next(l for l in result.lines if l.bucket_id == bucket_id)


DEBT = BucketSpec(4, "قسط متأخر", 2, CalcType.FIXED_AMOUNT, 10_000 * SAR, Frequency.IMMEDIATE)


def test_undated_fixed_takes_immediately_until_complete():
    """بدون تاريخ استحقاق: البند ياخذ حاجته فورًا لين يكتمل، وبعدها ما ياخذ شي."""
    (r1, r2, r3, r4), _ = run(
        [(8_000 * SAR, date(2026, 9, 1)), (20_000 * SAR, date(2026, 9, 5)),
         (1_000 * SAR, date(2026, 9, 6)), (1_000 * SAR, date(2026, 10, 1))],
        [TAX, DEBT, PROFIT],
    )
    # ضريبة 1,200 ثم القسط ياخذ الباقي 6,800 جزئيًا، والأرباح عجزها 400 يترحّل
    assert (line(r1, 4).allocated, line(r1, 4).status) == (6_800 * SAR, FundingStatus.PARTIAL)
    assert line(r1, 3).state_after.carried_deficit == 400 * SAR and r1.surplus == 0
    # القسط يكمل 3,200، والأرباح 1,000 + 400 المرحَّلة
    assert (line(r2, 4).allocated, line(r2, 4).status) == (3_200 * SAR, FundingStatus.FULLY_FUNDED)
    assert line(r2, 3).allocated == 1_400 * SAR
    assert r2.surplus == (20_000 - 3_000 - 3_200 - 1_400) * SAR
    # مكتمل: ما ياخذ شي، ولا حتى بالشهر الجاي
    assert line(r3, 4).status is FundingStatus.NOT_REQUIRED
    assert line(r4, 4).status is FundingStatus.NOT_REQUIRED


def test_dated_fixed_accrues_by_day_not_all_at_once():
    """إيجار شهري 10,000: يوم 10 من 30 يوم مستحق له الثلث بس، والباقي ينزل للبنود بعده."""
    (r1, r2), states = run([(9_000 * SAR, date(2026, 9, 10)), (12_000 * SAR, date(2026, 9, 30))])
    assert line(r1, 2).allocated == 333_334  # ceil(10,000 × 10/30) = 3,333.34
    assert line(r1, 2).status is FundingStatus.FULLY_FUNDED  # أخذ كل المستحق لين اليوم
    assert r1.surplus == 9_000 * SAR - 1_350 * SAR - 333_334 - 450 * SAR
    # آخر يوم قبل الاستحقاق: يكمل الهدف بالضبط، ويعوّض الأيام اللي ما جا فيها دخل
    assert line(r2, 2).allocated == 666_666
    assert states[2].funded == 10_000 * SAR


def test_semiannual_rent_paid_today_does_not_drain_tomorrows_income():
    """إيجار ٦٠,٠٠٠ كل ٦ شهور يستحق ١ مارس: دخل يوم ١ سبتمبر ما يروح كله للإيجار."""
    rent = BucketSpec(5, "الإيجار", 1, CalcType.FIXED_AMOUNT, 60_000 * SAR, Frequency.SEMIANNUAL,
                      due_date=date(2027, 3, 1))
    policy = [rent, PROFIT]
    (r1, r2, r3), states = run(
        [(50_000 * SAR, date(2026, 9, 1)), (100_000 * SAR, date(2027, 2, 28)), (1_000 * SAR, date(2027, 3, 1))],
        policy,
    )
    # الدورة ١٨١ يوم: أول يوم مستحق ceil(60,000 / 181) = 331.50 بس
    assert line(r1, 5).allocated == 33_150
    assert r1.surplus == 50_000 * SAR - 33_150 - 2_500 * SAR
    # آخر يوم قبل الاستحقاق: يكتمل ٦٠,٠٠٠
    assert line(r2, 5).state_after.funded == 60_000 * SAR
    # يوم الاستحقاق تبدأ دورة جديدة، والقديمة تنقفل بدون عجز
    closure = next(c for c in r3.closures if c.bucket_id == 5)
    assert (closure.period_key, closure.closed_deficit) == ("2026-09-01", 0)
    assert line(r3, 5).state_after.period_key == "2027-03-01"


def test_percentage_deficit_carries_until_paid():
    greedy = BucketSpec(9, "قسط", 1, CalcType.FIXED_AMOUNT, 1_000 * SAR, Frequency.IMMEDIATE)
    tax = BucketSpec(1, "الضريبة", 2, CalcType.PERCENTAGE, 1500, Frequency.IMMEDIATE)
    (r1, r2), _ = run(
        [(1_000 * SAR, date(2026, 9, 1)), (1_000 * SAR, date(2026, 9, 2))], [greedy, tax]
    )
    assert line(r1, 1).status == FundingStatus.UNFUNDED
    assert line(r1, 1).state_after.carried_deficit == 150 * SAR
    # الثانية: 150 نصيبها + 150 مرحَّلة
    assert line(r2, 1).required == 300 * SAR
    assert line(r2, 1).allocated == 300 * SAR
    assert line(r2, 1).state_after.carried_deficit == 0


def test_new_month_closes_period_with_deficit_and_resets():
    (r1, r2), states = run([(5_000 * SAR, date(2026, 9, 10)), (5_000 * SAR, date(2026, 10, 1))])
    rent_closure = next(c for c in r2.closures if c.bucket_id == 2)
    assert rent_closure.period_key == "2026-09-01"
    # ما جا دخل بعد يوم ١٠، فسبتمبر انقفل ناقص
    assert rent_closure.closed_deficit == 10_000 * SAR - 333_334
    # الإيجار يبدأ أكتوبر من الصفر: أول يوم من ٣١
    assert line(r2, 2).state_after.period_key == "2026-10-01"
    assert line(r2, 2).allocated == 32_259
    # الضريبة فورية: فترة دائمة، ما تنقفل
    assert not any(c.bucket_id == 1 for c in r2.closures)


def test_backdated_transaction_into_closed_period_is_rejected():
    _, states = run([(1_000 * SAR, date(2026, 10, 3))])
    with pytest.raises(ClosedPeriodError):
        allocate(POLICY, states, 1_000 * SAR, date(2026, 9, 30))


def test_backdated_within_open_period_is_accepted():
    _, states = run([(1_000 * SAR, date(2026, 9, 20))])
    result = allocate(POLICY, states, 1_000 * SAR, date(2026, 9, 3))
    assert line(result, 2).state_after.period_key == "2026-09-01"


def test_policy_change_mid_period_keeps_funded_and_uses_new_target():
    (r1,), states = run([(5_000 * SAR, date(2026, 9, 1))], [TAX, DEBT, PROFIT])
    raised = BucketSpec(4, "قسط متأخر", 2, CalcType.FIXED_AMOUNT, 12_000 * SAR, Frequency.IMMEDIATE)
    r2 = allocate([TAX, raised, PROFIT], states, 10_000 * SAR, date(2026, 9, 2))
    assert line(r2, 4).required == (12_000 - 4_250) * SAR


def test_rounding_goes_to_surplus():
    third = BucketSpec(1, "ثلث", 1, CalcType.PERCENTAGE, 3333, Frequency.IMMEDIATE)
    result = allocate([third], {}, 1, date(2026, 9, 1))  # هللة واحدة
    assert (line(result, 1).allocated, result.surplus) == (0, 1)
    assert line(result, 1).status == FundingStatus.NOT_REQUIRED


def test_non_positive_amount_rejected():
    with pytest.raises(ValueError):
        allocate(POLICY, {}, 0, date(2026, 9, 1))


@given(
    pcts=st.lists(st.integers(1, 3000), max_size=3),
    fixed=st.lists(st.integers(1, 5_000_000), max_size=3),
    txs=st.lists(
        st.tuples(st.integers(1, 10_000_000), st.dates(date(2026, 1, 1), date(2026, 12, 31))),
        min_size=1,
        max_size=15,
    ),
)
def test_invariant_allocated_plus_surplus_equals_amount(pcts, fixed, txs):
    buckets = [
        BucketSpec(i, f"p{i}", i, CalcType.PERCENTAGE, v, Frequency.IMMEDIATE) for i, v in enumerate(pcts)
    ] + [
        BucketSpec(100 + i, f"f{i}", 100 + i, CalcType.FIXED_AMOUNT, v, Frequency.MONTHLY)
        for i, v in enumerate(fixed)
    ]
    states = {}
    for amount, on in sorted(txs, key=lambda t: t[1]):
        result = allocate(buckets, states, amount, on)
        assert result.allocated_total + result.surplus == amount
        assert result.surplus >= 0
        for l in result.lines:
            assert 0 <= l.allocated <= l.required
            assert l.state_after.funded >= 0
            if l.state_after.target:
                assert l.state_after.funded <= l.state_after.target
        states = {l.bucket_id: l.state_after for l in result.lines}
