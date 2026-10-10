from datetime import datetime
from typing import Protocol

from app.graph.models import GraphFeatures, GraphNetwork, TransactionNode


class GraphRepository(Protocol):
    """Synchronous graph contract usable by local and future Neptune adapters."""

    def upsert_transaction(self, transaction: TransactionNode) -> None:
        """Replace by transaction ID, including edges; identical retries do nothing."""
        ...

    def get_transaction_network(self, transaction_id: str, limit: int = 50) -> GraphNetwork:
        """Return a connected breadth-first network, traversing edges both ways.

        Limit counts ALL nodes including the seed and must be positive. Neighbors
        are visited by (kind, identifier) order. Return all edges between selected
        nodes, keeping their original direction. A missing seed returns empty.
        This investigation view can include any event time; it is not a feature
        source and has no historical cutoff.
        """
        ...

    def get_historical_features(
        self, card_token: str, device_token: str | None, event_time: datetime,
    ) -> GraphFeatures:
        """Use only events in [event_time - window, event_time), in UTC.

        Windows are elapsed 24 hours or 7 days. Exclude ALL exact-time events
        (including the current transaction) and future events. Require aware
        event_time. Missing history returns zeros; absent device returns zero
        device features. Raise ValueError for mixed currencies in the card's
        7-day history. Never use labels or predictions.
        """
        ...
