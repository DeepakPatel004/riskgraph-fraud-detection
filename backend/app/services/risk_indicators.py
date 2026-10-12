"""Observed historical signals, independent of model decisions and persistence."""

from dataclasses import asdict
from decimal import Decimal

from app.graph.models import GraphFeatures
from app.schemas.fraud_inference import ValidatedGraphFeatures
from app.schemas.fraud_scoring import RiskIndicatorCode


RISK_INDICATOR_VERSION = 1


def observed_risk_indicators(amount: Decimal, features: GraphFeatures) -> list[RiskIndicatorCode]:
    """Use verified [start, cutoff) aggregates; never invent unavailable history.

    'Multiple' means at least two PRIOR transactions/associations. The amount
    comparison uses >= three times a positive seven-day card average, without
    rounding. This is a descriptive prototype rule, not a statistically derived
    threshold, model explanation, or fraud verdict. Order is fixed and stable.
    """
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0:
        raise ValueError("Indicators require a positive finite Decimal amount")
    if not isinstance(features, GraphFeatures):
        raise ValueError("Indicators require historical GraphFeatures")
    graph = ValidatedGraphFeatures.model_validate(asdict(features))
    indicators: list[RiskIndicatorCode] = []
    for observed, code in (
        (graph.card_txn_count_24h, "MULTIPLE_CARD_TRANSACTIONS_24H"),
        (graph.device_txn_count_24h, "MULTIPLE_DEVICE_TRANSACTIONS_24H"),
        (graph.card_unique_devices_7d, "CARD_MULTIPLE_DEVICES_7D"),
        (graph.device_unique_cards_7d, "DEVICE_MULTIPLE_CARDS_7D"),
    ):
        if observed >= 2:
            indicators.append(code)
    if graph.card_avg_amount_7d > 0 and amount >= Decimal(3) * graph.card_avg_amount_7d:
        indicators.append("AMOUNT_AT_LEAST_3X_CARD_AVERAGE_7D")
    return indicators
