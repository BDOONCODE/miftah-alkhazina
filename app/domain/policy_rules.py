"""Policy validation rules, and protection of protected items when editing."""
from __future__ import annotations

from collections.abc import Collection, Sequence

from .types import ANCHORED_MONTHS, BASIS_POINTS_FULL, BucketSpec, CalcType, Frequency

MIN_JUSTIFICATION_LENGTH = 20


def effective_rate(b: BucketSpec) -> int:
    """The item's share of an incoming amount in basis points (15% VAT = 13.04% of the amount)."""
    if b.calc_type is CalcType.VAT_INCLUSIVE:
        return b.value * BASIS_POINTS_FULL // (BASIS_POINTS_FULL + b.value)
    return b.value


def validate_policy(buckets: Sequence[BucketSpec]) -> list[str]:
    """Return a list of errors (in Arabic, shown to the user); an empty list means the policy is valid."""
    errors: list[str] = []
    if not buckets:
        return ["يجب أن تحتوي السياسة على بند واحد على الأقل"]

    priorities = [b.priority for b in buckets]
    if len(set(priorities)) != len(priorities):
        errors.append("يجب أن تكون لكل بند أولوية مختلفة")

    for b in buckets:
        label = f"البند «{b.name or '؟'}»"
        if not b.name.strip():
            errors.append("يجب أن يكون لكل بند اسم")
        if b.value <= 0:
            errors.append(f"{label}: يجب أن تكون القيمة أكبر من صفر")
        if b.calc_type.is_rate and b.value > BASIS_POINTS_FULL:
            errors.append(f"{label}: يجب ألا تتجاوز النسبة 100%")
        if b.frequency in ANCHORED_MONTHS and b.due_date is None:
            errors.append(f"{label}: حدد تاريخ الاستحقاق القادم")
        if b.frequency is Frequency.DAY_OF_MONTH and not (b.settlement_day and 1 <= b.settlement_day <= 31):
            errors.append(f"{label}: حدد يوم الاستحقاق من 1 إلى 31")

    pct_total = sum(effective_rate(b) for b in buckets if b.calc_type.is_rate)
    if pct_total > BASIS_POINTS_FULL:
        errors.append(f"مجموع النسب {pct_total / 100:.2f}% يتجاوز 100%")
    return errors


def protected_changes(
    old: Sequence[BucketSpec],
    new: Sequence[BucketSpec],
    funded_bucket_ids: Collection[int],
) -> list[str]:
    """Changes that touch protected items funded in the current period."""
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
        if (n.frequency, n.settlement_day, n.due_date) != (b.frequency, b.settlement_day, b.due_date):
            changes.append(f"تغيير جدولة البند المحمي «{b.name}»")
        if n.destination != b.destination:
            changes.append(f"تغيير مستفيد البند المحمي «{b.name}»")
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
