from datetime import datetime, timezone
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class TransactionCreate(BaseModel):
    transaction_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    card_token: str = Field(min_length=1, max_length=128)
    device_token: str | None = Field(default=None, max_length=128)
    event_time: datetime

    @field_validator("event_time")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event_time must include a timezone, for example Z")
        return value.astimezone(timezone.utc)


class TransactionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    transaction_id: str
    amount: Decimal
    currency: str
    card_token: str
    device_token: str | None
    event_time: datetime
    processing_status: str
    graph_sync_status: str
    risk_score: float | None
    risk_level: str | None


class TransactionPage(BaseModel):
    items: list[TransactionRead]
    total: int
    limit: int
    offset: int
