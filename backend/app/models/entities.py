import uuid
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import ForeignKey, Index, JSON, Numeric, String, Float, Integer, Text, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base
from app.core.types import UTCDateTime


def utc_now():
    return datetime.now(timezone.utc)


class Transaction(Base):
    __tablename__ = "transactions"
    transaction_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    card_token: Mapped[str] = mapped_column(String(128), nullable=False)
    device_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_time: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    features_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(12), nullable=True)
    processing_status: Mapped[str] = mapped_column(String(24), default="RECEIVED", nullable=False)
    graph_sync_status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_transactions_nonnegative_amount"),
        Index("ix_transactions_event_time", "event_time"),
        Index("ix_transactions_risk_time", "risk_level", "event_time"),
    )


class FraudAlert(Base):
    __tablename__ = "fraud_alerts"
    alert_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    transaction_id: Mapped[str] = mapped_column(String(64), ForeignKey("transactions.transaction_id"), unique=True, nullable=False)
    risk_score: Mapped[float] = mapped_column(Float, nullable=False)
    investigation_status: Mapped[str] = mapped_column(String(16), default="OPEN", nullable=False)
    risk_indicators: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False)
    __table_args__ = (Index("ix_alerts_status_created", "investigation_status", "created_at"),)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    transaction_id: Mapped[str] = mapped_column(String(64), ForeignKey("transactions.transaction_id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class GraphSyncJob(Base):
    __tablename__ = "graph_sync_jobs"
    job_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    transaction_id: Mapped[str] = mapped_column(String(64), ForeignKey("transactions.transaction_id"), unique=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    graph_population_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (Index("ix_graph_sync_status_due", "status", "next_attempt_at"),)
