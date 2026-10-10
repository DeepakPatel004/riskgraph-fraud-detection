from collections import defaultdict, deque
from datetime import datetime, timedelta
from decimal import Decimal

from app.graph.models import (
    CardNode, DeviceNode, GraphFeatures, GraphNetwork, GraphNode,
    GraphRelationship, NodeRef, TransactionNode, as_utc,
)


class InMemoryGraphRepository:
    """Small, process-local reference adapter; data disappears on restart.

    Scans favor clarity over scalability. Not intended for concurrent writes;
    callers must serialize access if sharing an instance across threads.
    """

    def __init__(self) -> None:
        self._nodes: dict[NodeRef, GraphNode] = {}
        self._relationships: set[GraphRelationship] = set()

    def upsert_transaction(self, transaction: TransactionNode) -> None:
        previous_edges = {edge for edge in self._relationships if edge.source == transaction.ref}
        self._relationships.difference_update(previous_edges)
        self._nodes[transaction.ref] = transaction
        card = CardNode(transaction.card_token)
        self._nodes[card.ref] = card
        self._relationships.add(GraphRelationship(transaction.ref, card.ref, "USES_CARD"))
        if transaction.device_token is not None:
            device = DeviceNode(transaction.device_token)
            self._nodes[device.ref] = device
            self._relationships.add(GraphRelationship(transaction.ref, device.ref, "USES_DEVICE"))
        # A corrected transaction must not leave disconnected, unused token nodes.
        for edge in previous_edges:
            if not any(current.target == edge.target for current in self._relationships):
                self._nodes.pop(edge.target, None)

    def get_transaction_network(self, transaction_id: str, limit: int = 50) -> GraphNetwork:
        """See GraphRepository: deterministic BFS capped by total node count."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        seed = NodeRef("Transaction", transaction_id)
        if seed not in self._nodes:
            return GraphNetwork()
        neighbors: dict[NodeRef, set[NodeRef]] = defaultdict(set)
        for edge in self._relationships:
            neighbors[edge.source].add(edge.target)
            neighbors[edge.target].add(edge.source)
        selected = [seed]
        seen = {seed}
        queue = deque([seed])
        while queue and len(selected) < limit:
            current = queue.popleft()
            for neighbor in sorted(neighbors[current], key=lambda ref: (ref.kind, ref.identifier)):
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                selected.append(neighbor)
                queue.append(neighbor)
                if len(selected) == limit:
                    break
        edges = tuple(sorted(
            (edge for edge in self._relationships if edge.source in seen and edge.target in seen),
            key=lambda edge: (edge.source.identifier, edge.kind, edge.target.identifier),
        ))
        return GraphNetwork(tuple(self._nodes[ref] for ref in selected), edges)

    def get_historical_features(
        self, card_token: str, device_token: str | None, event_time: datetime,
    ) -> GraphFeatures:
        """Calculate elapsed UTC windows with inclusive lower/exclusive upper bounds."""
        cutoff = as_utc(event_time)
        lower_7d = cutoff - timedelta(days=7)
        lower_24h = cutoff - timedelta(hours=24)
        history = [node for node in self._nodes.values()
                   if isinstance(node, TransactionNode) and lower_7d <= node.event_time < cutoff]
        card_history = [node for node in history if node.card_token == card_token]
        device_history = [node for node in history
                          if device_token is not None and node.device_token == device_token]
        if len({node.currency for node in card_history}) > 1:
            raise ValueError("Cannot average mixed currencies in card history")
        average = (sum((node.amount for node in card_history), Decimal("0")) / len(card_history)
                   if card_history else Decimal("0"))
        return GraphFeatures(
            card_txn_count_24h=sum(node.event_time >= lower_24h for node in card_history),
            device_txn_count_24h=sum(node.event_time >= lower_24h for node in device_history),
            card_unique_devices_7d=len({node.device_token for node in card_history
                                       if node.device_token is not None}),
            device_unique_cards_7d=len({node.card_token for node in device_history}),
            card_avg_amount_7d=average,
        )
