"""Small visualization contract; source tokens and model data are never serialized."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TransactionGraphProperties(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: str
    currency: str


class TokenGraphProperties(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExplorerNode(BaseModel):
    id: str
    type: Literal["Transaction", "Card", "Device"]
    label: str
    properties: TransactionGraphProperties | TokenGraphProperties


class ExplorerEdge(BaseModel):
    source: str
    target: str
    type: Literal["USES_CARD", "USES_DEVICE"]


class GraphExplorerResponse(BaseModel):
    transaction_id: str
    nodes: list[ExplorerNode]
    edges: list[ExplorerEdge]
    node_count: int
    edge_count: int
    truncated: bool | None = Field(
        description="False when traversal exhausted below the limit; null when the cap was reached and truncation is unknown.",
    )
