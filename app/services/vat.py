"""ضريبة القيمة المضافة: إلزامية ويحسبها النظام بنفسه، مو بند في السياسة.

كل مبلغ يدخل شامل الضريبة، فيتحجز منه أول شي ١٥ ÷ ١١٥ قبل أي بند.
تتجمّع لكل ربع سنة ميلادي، وتتحوّل لحساب الضريبة أول يوم بعد نهاية الربع
(قبل موعد الإقرار نهاية الشهر اللي بعده). المحاسب يحدد حساب الضريبة بس.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from ..domain.types import BASIS_POINTS_FULL, BucketSpec, CalcType, Frequency
from ..models import Bucket, Entity

VAT_NAME = "ضريبة القيمة المضافة"
VAT_RATE = 1500  # ١٥٪ بنقاط الأساس
VAT_PRIORITY = 0  # قبل كل بنود السياسة (أولوياتها تبدأ من ١)
QUARTER_ANCHOR = date(2026, 1, 1)  # الأرباع ميلادية: يناير، أبريل، يوليو، أكتوبر
# نصيب الضريبة من المبلغ الوارد: ١٥ ÷ ١١٥ = ١٣٫٠٤٪
VAT_SHARE = VAT_RATE * BASIS_POINTS_FULL // (BASIS_POINTS_FULL + VAT_RATE)


@dataclass
class VatItem:
    """نفس شكل بند السياسة، عشان الصرف واللوحة يتعاملون معه مثل أي بند."""

    bucket_id: int
    destination_account_id: int | None
    name: str = VAT_NAME
    priority: int = VAT_PRIORITY
    calc_type: CalcType = CalcType.VAT_INCLUSIVE
    value: int = VAT_RATE
    frequency: Frequency = Frequency.QUARTERLY
    settlement_day: int | None = None
    due_date: date | None = QUARTER_ANCHOR
    protected: bool = True

    def spec(self) -> BucketSpec:
        return BucketSpec(
            bucket_id=self.bucket_id, name=self.name, priority=self.priority, calc_type=self.calc_type,
            value=self.value, frequency=self.frequency, due_date=self.due_date,
            destination=str(self.destination_account_id or ""), protected=True,
        )


def vat_item(session: Session, entity_id: int, *, create: bool = True) -> VatItem | None:
    """بند الضريبة للشركة. ينفتح له رصيد مستقل أول مرة (create)، ويبقى نفسه دائمًا."""
    entity = session.get(Entity, entity_id)
    if entity.vat_bucket_id is None:
        if not create:
            return None
        bucket = Bucket(entity_id=entity_id, system_name=VAT_NAME)
        session.add(bucket)
        session.flush()
        entity.vat_bucket_id = bucket.id
    return VatItem(bucket_id=entity.vat_bucket_id, destination_account_id=entity.vat_account_id)


def is_vat_bucket(session: Session, entity_id: int, bucket_id: int) -> bool:
    return session.get(Entity, entity_id).vat_bucket_id == bucket_id
