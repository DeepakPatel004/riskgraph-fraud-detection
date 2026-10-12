"""Public alert fields deliberately exclude card/device identifiers and features."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


InvestigationStatus = Literal["OPEN", "IN_REVIEW", "CLOSED"]


class AlertUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    investigation_status: InvestigationStatus


class AlertRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    alert_id: str
    transaction_id: str
    risk_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    # Existing alert rows may contain descriptive strings from earlier versions.
    risk_indicators: list[str]
    investigation_status: InvestigationStatus
    created_at: datetime
    updated_at: datetime


class AlertPage(BaseModel):
    items: list[AlertRead]
    total: int
    limit: int
    offset: int
