"""Count-only dashboard responses; no payment identifiers or model objects."""

from datetime import date
from typing import Annotated

from pydantic import BaseModel, Field


Count = Annotated[int, Field(ge=0)]


class DashboardSummary(BaseModel):
    total_transactions: Count
    scored_transactions: Count
    unscored_transactions: Count
    high_risk_transactions: Count
    low_risk_transactions: Count
    total_alerts: Count
    open_alerts: Count
    in_review_alerts: Count
    closed_alerts: Count


class RiskDistribution(BaseModel):
    LOW: Count
    HIGH: Count
    UNSCORED: Count


class AlertStatusCounts(BaseModel):
    OPEN: Count
    IN_REVIEW: Count
    CLOSED: Count


class DailyTransactionCount(BaseModel):
    date: date
    transaction_count: Count


class TransactionTrends(BaseModel):
    start_date: date | None
    end_date: date | None
    items: list[DailyTransactionCount]
