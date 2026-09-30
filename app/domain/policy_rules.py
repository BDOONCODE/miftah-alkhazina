"""قواعد التحقق من السياسة، وحماية البنود المحمية عند التعديل."""
from __future__ import annotations

from collections.abc import Collection, Sequence

from .types import BASIS_POINTS_FULL, BucketSpec, CalcType, Frequency

MIN_JUSTIFICATION_LENGTH = 20


def validate_policy(buckets: Sequence[BucketSpec]) -> list[str]:
    """يرجّع قائمة أخطاء بالعربي؛ القائمة الفاضية تعني السياسة سليمة."""
    errors: list[str] = []
    if not buckets:
        return ["السياسة لازم يكون فيها بند واحد على الأقل"]

    priorities = [b.priority for b in buckets]
    if len(set(priorities)) != len(priorities):
        errors.append("كل بند لازم تكون له أولوية مختلفة")

    for b in buckets:
        label = f"البند «{b.name or '؟'}»"
        if not b.name.strip():
            errors.append("كل بند لازم يكون له اسم")
        if b.value <= 0:
            errors.append(f"{label}: القيمة لازم تكون أكبر من صفر")
        if b.calc_type is CalcType.PERCENTAGE and b.value > BASIS_POINTS_FULL:
            errors.append(f"{label}: النسبة ما تتجاوز 100%")
        if b.calc_type is CalcType.FIXED_AMOUNT and b.frequency is Frequency.IMMEDIATE:
            errors.append(f"{label}: المبلغ الثابت يحتاج فترة (يومي أو أسبوعي أو شهري…)، ما يصلح فوري")
        if b.frequency is Frequency.DAY_OF_MONTH and not (b.settlement_day and 1 <= b.settlement_day <= 31):
            errors.append(f"{label}: حدد يوم الاستحقاق بين 1 و31")

    pct_total = sum(b.value for b in buckets if b.calc_type is CalcType.PERCENTAGE)
    if pct_total > BASIS_POINTS_FULL:
        errors.append(f"مجموع النسب {pct_total / 100:.2f}% يتجاوز 100%")
    return errors


def protected_changes(
    old: Sequence[BucketSpec],
    new: Sequence[BucketSpec],
    funded_bucket_ids: Collection[int],
) -> list[str]:
    """التعديلات اللي تمس بنود محمية تموّلت في الفترة الحالية."""
    new_by_id = {b.bucket_id: b for b in new}
    changes: list[str] = []
    for b in old:
        if not b.protected or b.bucket_id not in funded_bucket_ids:
            continue
        n = new_by_id.get(b.bucket_id)
        if n is None:
            changes.append(f"حذف البند المحمي «{b.name}»")
            continue
        if (n.calc_type, n.value) != (b.calc_type, b.value):
            changes.append(f"تغيير قيمة البند المحمي «{b.name}»")
        if n.priority != b.priority:
            changes.append(f"تغيير أولوية البند المحمي «{b.name}»")
        if n.frequency != b.frequency or n.settlement_day != b.settlement_day:
            changes.append(f"تغيير جدولة البند المحمي «{b.name}»")
        if n.destination != b.destination:
            changes.append(f"تغيير وجهة البند المحمي «{b.name}»")
        if not n.protected:
            changes.append(f"إلغاء حماية البند «{b.name}»")
    return changes


def justification_error(changes: Sequence[str], justification: str | None) -> str | None:
    if not changes:
        return None
    if len((justification or "").strip()) < MIN_JUSTIFICATION_LENGTH:
        return (
            f"هذه التعديلات تمس بنودًا محمية وتحتاج تبريرًا مكتوبًا ({MIN_JUSTIFICATION_LENGTH} حرفًا على الأقل): "
            + "، ".join(changes)
        )
    return None
