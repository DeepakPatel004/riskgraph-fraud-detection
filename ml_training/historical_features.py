"""Incremental equivalent of GraphRepository's historical feature calculations."""

from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Sequence

from backend.app.graph.models import GraphFeatures, TransactionNode


@dataclass
class CardHistory:
    count: int = 0
    amount_sum: Decimal = Decimal("0")
    currencies: Counter = field(default_factory=Counter)
    devices: Counter = field(default_factory=Counter)


def decrement(counts: Counter, key: str) -> None:
    """Remove zero entries so distinct counts and memory use remain correct."""
    counts[key] -= 1
    if counts[key] == 0:
        del counts[key]


class HistoricalFeatureBuilder:
    """Append-only, unique-ID, chronological input; not an upsert repository.

    Each event enters and leaves each queue once. Frequency counters preserve
    repeated card/device associations until their final in-window event expires.
    State holds only the active seven-day window, never targets or predictions.
    Submit a complete timestamp group in one call, including across split edges.
    """

    def __init__(self):
        self.window_24h = deque()
        self.window_7d = deque()
        self.card_counts_24h = Counter()
        self.device_counts_24h = Counter()
        self.cards: dict[str, CardHistory] = {}
        self.devices: dict[str, Counter] = {}
        self.last_time = None

    def _expire(self, event_time):
        # Strict < preserves the inclusive lower boundary of [T-window, T).
        while self.window_24h and self.window_24h[0].event_time < event_time - timedelta(hours=24):
            node = self.window_24h.popleft()
            decrement(self.card_counts_24h, node.card_token)
            if node.device_token is not None:
                decrement(self.device_counts_24h, node.device_token)
        while self.window_7d and self.window_7d[0].event_time < event_time - timedelta(days=7):
            node = self.window_7d.popleft()
            card = self.cards[node.card_token]
            card.count -= 1
            card.amount_sum -= node.amount
            decrement(card.currencies, node.currency)
            if node.device_token is not None:
                decrement(card.devices, node.device_token)
                device = self.devices[node.device_token]
                decrement(device, node.card_token)
                if not device:
                    del self.devices[node.device_token]
            if card.count == 0:
                del self.cards[node.card_token]

    def _features(self, node: TransactionNode) -> GraphFeatures:
        card = self.cards.get(node.card_token)
        if card is not None and len(card.currencies) > 1:
            # Same rule as GraphRepository: historical currencies only, not the
            # current currency, determine whether the historical average is valid.
            raise ValueError("Cannot average mixed currencies in card history")
        return GraphFeatures(
            card_txn_count_24h=self.card_counts_24h[node.card_token],
            device_txn_count_24h=self.device_counts_24h[node.device_token] if node.device_token else 0,
            card_unique_devices_7d=len(card.devices) if card else 0,
            device_unique_cards_7d=len(self.devices.get(node.device_token, ())),
            card_avg_amount_7d=card.amount_sum / card.count if card else Decimal("0"),
        )

    def _add(self, node: TransactionNode):
        self.window_24h.append(node)
        self.window_7d.append(node)
        self.card_counts_24h[node.card_token] += 1
        card = self.cards.setdefault(node.card_token, CardHistory())
        card.count += 1
        card.amount_sum += node.amount
        card.currencies[node.currency] += 1
        if node.device_token is not None:
            self.device_counts_24h[node.device_token] += 1
            card.devices[node.device_token] += 1
            self.devices.setdefault(node.device_token, Counter())[node.card_token] += 1

    def process_group(self, nodes: Sequence[TransactionNode]) -> list[GraphFeatures]:
        """Calculate every row first, then add the entire equal-time group.

        Reject time regressions and repeated group times rather than accidentally
        including a same-time row. Empty groups do nothing. Decimal sums retain
        cents exactly; division uses the existing Decimal context (normally 28
        significant digits), without rounding the resulting average to cents.
        """
        if not nodes:
            return []
        event_time = nodes[0].event_time
        if any(node.event_time != event_time for node in nodes):
            raise ValueError("A group must contain exactly one timestamp")
        if self.last_time is not None and event_time <= self.last_time:
            raise ValueError("Timestamp groups must be strictly increasing")
        self._expire(event_time)
        result = [self._features(node) for node in nodes]
        for node in nodes:
            self._add(node)
        self.last_time = event_time
        return result
