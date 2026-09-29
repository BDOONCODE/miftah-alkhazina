"""جداول قاعدة البيانات. المبالغ BigInteger بالهللة، والنسب بنقاط الأساس.

السجلات المالية (transactions, allocation_events, period_closures, audit_log)
تُضاف فقط — التصحيح يكون بقيد عكسي، ما في تعديل ولا حذف.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from .db import Base
from .domain.types import CalcType, Frequency, FundingStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """يخزّن الوقت دائمًا بتوقيت UTC ويرجّعه مع المنطقة الزمنية.

    SQLite ما يحفظ المنطقة الزمنية، فبدون هذا ترجع الأوقات بدون منطقة وتنعرض غلط،
    ومقارنة الفترات تختلط. PostgreSQL يتعامل معه بنفس الطريقة.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("الوقت لازم يكون بمنطقة زمنية")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        return value.replace(tzinfo=timezone.utc) if value is not None else None


def _enum(cls):
    return Enum(cls, native_enum=False, length=20, values_callable=lambda e: [m.value for m in e])


class Role(StrEnum):
    ADMIN = "admin"
    ACCOUNTANT = "accountant"
    APPROVER = "approver"


class PolicyStatus(StrEnum):
    DRAFT = "draft"
    PENDING = "pending"
    ACTIVE = "active"
    ARCHIVED = "archived"
    REJECTED = "rejected"


class TransactionSource(StrEnum):
    MANUAL = "manual"
    OPEN_BANKING = "open_banking"


# ---------------------------------------------------------------- المستخدمين والكيانات


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    full_name: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(_enum(Role))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    entities: Mapped[list[Entity]] = relationship(secondary="user_entities", back_populates="users")


class Entity(Base):
    __tablename__ = "entities"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(32), default="company")
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("entities.id"))  # محجوز للهرمية لاحقًا
    currency: Mapped[str] = mapped_column(String(3), default="SAR")
    cr_number: Mapped[str] = mapped_column(String(32), default="")  # السجل التجاري
    vat_number: Mapped[str] = mapped_column(String(32), default="")  # الرقم الضريبي
    bank_name: Mapped[str] = mapped_column(String(64), default="")
    iban: Mapped[str] = mapped_column(String(34), default="")
    contact_name: Mapped[str] = mapped_column(String(128), default="")
    contact_phone: Mapped[str] = mapped_column(String(32), default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    users: Mapped[list[User]] = relationship(secondary="user_entities", back_populates="entities")

    @property
    def reviewers(self) -> list[User]:
        return [u for u in self.users if u.role is Role.APPROVER]


class UserEntity(Base):
    __tablename__ = "user_entities"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True)


# ---------------------------------------------------------------- السياسات


class Bucket(Base):
    """هوية ثابتة للبند عبر نسخ السياسة، عشان يحتفظ برصيده لما تتغير السياسة."""

    __tablename__ = "buckets"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Policy(Base):
    __tablename__ = "policies"
    __table_args__ = (UniqueConstraint("entity_id", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[PolicyStatus] = mapped_column(_enum(PolicyStatus), default=PolicyStatus.DRAFT)
    notes: Mapped[str] = mapped_column(Text, default="")
    justification: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    decision_note: Mapped[str] = mapped_column(Text, default="")

    buckets: Mapped[list[PolicyBucket]] = relationship(
        back_populates="policy", cascade="all, delete-orphan", order_by="PolicyBucket.priority"
    )
    creator: Mapped[User] = relationship(foreign_keys=[created_by])
    decider: Mapped[User | None] = relationship(foreign_keys=[decided_by])


class PolicyBucket(Base):
    __tablename__ = "policy_buckets"
    __table_args__ = (UniqueConstraint("policy_id", "bucket_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("policies.id", ondelete="CASCADE"))
    bucket_id: Mapped[int] = mapped_column(ForeignKey("buckets.id"))
    name: Mapped[str] = mapped_column(String(128))
    priority: Mapped[int] = mapped_column(Integer)
    calc_type: Mapped[CalcType] = mapped_column(_enum(CalcType))
    value: Mapped[int] = mapped_column(BigInteger)
    frequency: Mapped[Frequency] = mapped_column(_enum(Frequency))
    settlement_day: Mapped[int | None] = mapped_column(Integer)
    destination: Mapped[str] = mapped_column(String(128), default="")
    protected: Mapped[bool] = mapped_column(Boolean, default=False)

    policy: Mapped[Policy] = relationship(back_populates="buckets")


# ---------------------------------------------------------------- المعاملات والتخصيص


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), index=True)
    amount: Mapped[int] = mapped_column(BigInteger)  # سالب للقيد العكسي
    surplus: Mapped[int] = mapped_column(BigInteger, default=0)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    source: Mapped[TransactionSource] = mapped_column(_enum(TransactionSource), default=TransactionSource.MANUAL)
    external_ref: Mapped[str | None] = mapped_column(String(128), unique=True)
    reversal_of: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"), unique=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("policies.id"))
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    events: Mapped[list[AllocationEvent]] = relationship(back_populates="transaction", order_by="AllocationEvent.id")
    creator: Mapped[User] = relationship()


class AllocationEvent(Base):
    __tablename__ = "allocation_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), index=True)
    bucket_id: Mapped[int] = mapped_column(ForeignKey("buckets.id"), index=True)
    period_key: Mapped[str] = mapped_column(String(10))
    required: Mapped[int] = mapped_column(BigInteger)
    allocated: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[FundingStatus] = mapped_column(_enum(FundingStatus))
    funded_after: Mapped[int] = mapped_column(BigInteger)
    carried_deficit_after: Mapped[int] = mapped_column(BigInteger)
    # لقطة الحالة قبل المعاملة — تُستخدم لاسترجاعها بالقيد العكسي
    before_period_key: Mapped[str | None] = mapped_column(String(10))
    before_target: Mapped[int | None] = mapped_column(BigInteger)
    before_funded: Mapped[int | None] = mapped_column(BigInteger)
    before_carried_deficit: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    transaction: Mapped[Transaction] = relationship(back_populates="events")


class BucketPeriodState(Base):
    """الحالة الحية لكل بند في فترته الحالية (صف واحد لكل بند)."""

    __tablename__ = "bucket_period_state"
    bucket_id: Mapped[int] = mapped_column(ForeignKey("buckets.id"), primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), index=True)
    period_key: Mapped[str] = mapped_column(String(10))
    target: Mapped[int] = mapped_column(BigInteger, default=0)
    funded: Mapped[int] = mapped_column(BigInteger, default=0)
    carried_deficit: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class PeriodClosure(Base):
    __tablename__ = "period_closures"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), index=True)
    bucket_id: Mapped[int] = mapped_column(ForeignKey("buckets.id"), index=True)
    period_key: Mapped[str] = mapped_column(String(10))
    target: Mapped[int] = mapped_column(BigInteger)
    funded: Mapped[int] = mapped_column(BigInteger)
    closed_deficit: Mapped[int] = mapped_column(BigInteger)
    # الإقفال يصير إما بمعاملة في فترة جديدة، أو باعتماد سياسة حذفت البند
    closed_by_transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"))
    closed_by_policy_id: Mapped[int | None] = mapped_column(ForeignKey("policies.id"))
    voided: Mapped[bool] = mapped_column(Boolean, default=False)  # ألغاها قيد عكسي
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


# ---------------------------------------------------------------- اللوحة


class Dashboard(Base):
    __tablename__ = "dashboards"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), unique=True)
    title: Mapped[str] = mapped_column(String(128))
    initialized: Mapped[bool] = mapped_column(Boolean, default=False)  # القالب الافتراضي انطبق مرة
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    widgets: Mapped[list[DashboardWidget]] = relationship(
        back_populates="dashboard", cascade="all, delete-orphan", order_by="DashboardWidget.id"
    )


class DashboardWidget(Base):
    __tablename__ = "dashboard_widgets"
    id: Mapped[int] = mapped_column(primary_key=True)
    dashboard_id: Mapped[int] = mapped_column(ForeignKey("dashboards.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(128))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    x: Mapped[int] = mapped_column(Integer, default=0)
    y: Mapped[int] = mapped_column(Integer, default=0)
    w: Mapped[int] = mapped_column(Integer, default=3)
    h: Mapped[int] = mapped_column(Integer, default=2)

    dashboard: Mapped[Dashboard] = relationship(back_populates="widgets")


class CustomMetric(Base):
    __tablename__ = "custom_metrics"
    __table_args__ = (UniqueConstraint("entity_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    formula: Mapped[str] = mapped_column(Text)
    fmt: Mapped[str] = mapped_column(String(16), default="number")  # sar | percent | number
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


# ---------------------------------------------------------------- التدقيق


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    entity_id: Mapped[int | None] = mapped_column(ForeignKey("entities.id"), index=True)
    action: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)

    user: Mapped[User | None] = relationship()
