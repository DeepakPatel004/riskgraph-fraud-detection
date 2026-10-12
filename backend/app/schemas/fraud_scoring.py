"""Internal scoring contracts, independent of the existing transaction API."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, StrictStr, field_validator

from app.graph.models import as_utc
from app.schemas.fraud_inference import (
    FraudInferenceResult, TransactionModelAttributes, ValidatedGraphFeatures,
)


RiskIndicatorCode = Literal[
    "MULTIPLE_CARD_TRANSACTIONS_24H", "MULTIPLE_DEVICE_TRANSACTIONS_24H",
    "CARD_MULTIPLE_DEVICES_7D", "DEVICE_MULTIPLE_CARDS_7D",
    "AMOUNT_AT_LEAST_3X_CARD_AVERAGE_7D",
]


class FraudScoringRequest(TransactionModelAttributes):
    """Flat source attributes; inherited categorical validation preserves nulls.

    Tokens must be pseudonymous. Nonblank tokens are preserved exactly, rather
    than stripped into a potentially different graph identity.
    """

    transaction_id: StrictStr = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2, allow_inf_nan=False)
    currency: Literal["USD"]
    card_token: StrictStr = Field(min_length=1, max_length=128)
    device_token: StrictStr | None = Field(default=None, max_length=128)
    event_time: datetime

    # Override the inherited wildcard: its missing-marker rule is for categories,
    # not amounts, timestamps or identifiers added by this subclass.
    @field_validator("*")
    @classmethod
    def require_source_value(cls, value, info):
        if info.field_name in TransactionModelAttributes.model_fields:
            return TransactionModelAttributes.require_source_value(value)
        return value

    @field_validator("card_token")
    @classmethod
    def require_card_token(cls, value):
        if not value.strip():
            raise ValueError("Card token must not be blank")
        return value

    @field_validator("device_token", mode="before")
    @classmethod
    def normalize_missing_device(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("event_time")
    @classmethod
    def normalize_event_time(cls, value):
        return as_utc(value)


class HistoricalGraphMetadata(ValidatedGraphFeatures):
    """Actual aggregates plus their UTC windows; contains no card/device tokens."""

    cutoff: datetime
    window_start_24h: datetime
    window_start_7d: datetime
    boundary_policy: Literal["[start, end)"] = "[start, end)"
    currency: Literal["USD"] = "USD"
    freshness: Literal["VERIFIED"] = "VERIFIED"


class FraudScoringResult(FraudInferenceResult):
    transaction_id: str
    historical_features: HistoricalGraphMetadata
    risk_indicators: list[RiskIndicatorCode] = Field(
        default_factory=list, description="Observed historical signals, not model explanations or proof of fraud.",
    )


class FraudScoringResponse(FraudInferenceResult):
    """Saved prediction plus the transaction's current outbox state."""

    transaction_id: str
    graph_sync_status: Literal["PENDING", "PROCESSING", "SYNCED", "FAILED"]
    risk_indicators: list[RiskIndicatorCode] = Field(
        default_factory=list, description="Recorded historical signals; an empty list does not prove legitimacy.",
    )


class ScoringErrorMetadata(BaseModel):
    code: str
    retryable: bool


class ScoringErrorResponse(BaseModel):
    detail: str | list[dict]
    error: ScoringErrorMetadata
