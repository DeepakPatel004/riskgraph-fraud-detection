"""Internal inference inputs; deliberately separate from public transaction APIs."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator


class TransactionModelAttributes(BaseModel):
    """Absent source attributes remain null; never guess a product or match flag."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    ProductCD: StrictStr | None = None
    M1: StrictStr | None = None
    M2: StrictStr | None = None
    M3: StrictStr | None = None
    M4: StrictStr | None = None
    M5: StrictStr | None = None
    M6: StrictStr | None = None
    M7: StrictStr | None = None
    M8: StrictStr | None = None
    M9: StrictStr | None = None

    @field_validator("*")
    @classmethod
    def require_source_value(cls, value):
        if value is not None and (not value.strip() or value == "__MISSING__"):
            raise ValueError("Use null for missing source attributes")
        return value


class InferenceTransaction(BaseModel):
    """No tokens, IDs, dates or labels. Currency is a guard, not a predictor.

    Phase 4 must derive device_missing from validated device presence, and supply
    historical GraphFeatures with the transaction's exclusive event-time cutoff.
    The current model supports only its explicit prototype USD assumption.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2, allow_inf_nan=False)
    currency: Literal["USD"]
    device_missing: StrictBool
    attributes: TransactionModelAttributes = Field(default_factory=TransactionModelAttributes)


class ValidatedGraphFeatures(BaseModel):
    """Validate the existing GraphFeatures dataclass, which itself is not strict."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    card_txn_count_24h: int = Field(ge=0)
    device_txn_count_24h: int = Field(ge=0)
    card_unique_devices_7d: int = Field(ge=0)
    device_unique_cards_7d: int = Field(ge=0)
    card_avg_amount_7d: Decimal = Field(ge=0, allow_inf_nan=False)


class FraudInferenceResult(BaseModel):
    """Serializable values only; no estimator, preprocessing object or tokens."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    risk_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    risk_level: Literal["LOW", "HIGH"]
    model_version: str = Field(min_length=1)
