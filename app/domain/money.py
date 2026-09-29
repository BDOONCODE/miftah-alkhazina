"""تحويل المبالغ والنسب بين النص المُدخل والتخزين الداخلي (هللات / نقاط أساس)."""
from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, InvalidOperation

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")


def _to_decimal(text: str) -> Decimal:
    cleaned = text.translate(_ARABIC_DIGITS).replace(",", "").replace(" ", "").strip()
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"رقم غير صالح: {text!r}") from exc


def parse_amount(text: str) -> int:
    """'1,234.56' → 123456 هللة. أكثر من خانتين عشريتين مرفوض."""
    value = _to_decimal(text)
    halalas = value * 100
    if halalas != halalas.to_integral_value():
        raise ValueError("المبلغ ما يقبل أكثر من خانتين عشريتين")
    return int(halalas)


def parse_percent(text: str) -> int:
    """'15.5' → 1550 نقطة أساس."""
    value = _to_decimal(text) * 100
    if value != value.to_integral_value():
        raise ValueError("النسبة ما تقبل أكثر من خانتين عشريتين")
    return int(value)


def format_amount(halalas: int) -> str:
    sign = "-" if halalas < 0 else ""
    value = (Decimal(abs(halalas)) / 100).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return f"{sign}{value:,.2f}"


def format_percent(basis_points: int) -> str:
    return f"{Decimal(basis_points) / 100:g}%"
