from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def as_utc(value: datetime) -> datetime:
    """Require an exact instant; never guess the timezone of a naive value."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Graph timestamps must include a timezone")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class NodeRef:
    """Label plus identifier keeps equal card/device/transaction IDs distinct."""

    kind: Literal["Transaction", "Card", "Device"]
    identifier: str


class TransactionNode(BaseModel):
    """Graph input with pseudonymous tokens only; no labels or risk predictions.

    Construct from selected SQL/Pydantic fields explicitly. Extra fields are
    rejected so unrelated payment details cannot silently enter the graph.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    transaction_id: str = Field(min_length=1, max_length=64)
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    card_token: str = Field(min_length=1, max_length=128)
    device_token: str | None = Field(default=None, min_length=1, max_length=128)
    event_time: datetime

    @field_validator("event_time")
    @classmethod
    def normalize_event_time(cls, value: datetime) -> datetime:
        return as_utc(value)

    @property
    def ref(self) -> NodeRef:
        return NodeRef("Transaction", self.transaction_id)


@dataclass(frozen=True)
class CardNode:
    """Contains a pseudonymous card token, never a raw payment card number."""

    card_token: str

    @property
    def ref(self) -> NodeRef:
        return NodeRef("Card", self.card_token)


@dataclass(frozen=True)
class DeviceNode:
    device_token: str

    @property
    def ref(self) -> NodeRef:
        return NodeRef("Device", self.device_token)


GraphNode = TransactionNode | CardNode | DeviceNode


@dataclass(frozen=True)
class GraphRelationship:
    source: NodeRef
    target: NodeRef
    kind: Literal["USES_CARD", "USES_DEVICE"]


@dataclass(frozen=True)
class GraphNetwork:
    nodes: tuple[GraphNode, ...] = ()
    relationships: tuple[GraphRelationship, ...] = ()


@dataclass(frozen=True)
class GraphFeatures:
    """Empty histories yield zero; absent devices yield zero device features.

    Average is a Decimal in the card history's currency, without cent rounding.
    Mixed-currency card histories are rejected rather than averaged together.
    """

    card_txn_count_24h: int = 0
    device_txn_count_24h: int = 0
    card_unique_devices_7d: int = 0
    device_unique_cards_7d: int = 0
    card_avg_amount_7d: Decimal = Decimal("0")
