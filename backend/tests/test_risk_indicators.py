"""Deterministic observations; no classifier, persistence or external services."""

from decimal import Decimal

import pytest

from app.graph.models import GraphFeatures
from app.services.risk_indicators import observed_risk_indicators


@pytest.mark.parametrize("count,expected", [(0, []), (1, []), (2, [
    "MULTIPLE_CARD_TRANSACTIONS_24H", "MULTIPLE_DEVICE_TRANSACTIONS_24H",
    "CARD_MULTIPLE_DEVICES_7D", "DEVICE_MULTIPLE_CARDS_7D",
])])
def test_multiple_means_at_least_two_prior_observations(count, expected):
    features = GraphFeatures(count, count, count, count, Decimal("10"))
    assert observed_risk_indicators(Decimal("10"), features) == expected


@pytest.mark.parametrize("amount,expected", [("59.99", []), ("60", [
    "AMOUNT_AT_LEAST_3X_CARD_AVERAGE_7D"]), ("60.01", ["AMOUNT_AT_LEAST_3X_CARD_AVERAGE_7D"])])
def test_amount_comparison_is_inclusive_and_decimal_precise(amount, expected):
    assert observed_risk_indicators(Decimal(amount), GraphFeatures(card_avg_amount_7d=Decimal("20"))) == expected


def test_empty_history_does_not_flag_large_amount_or_missing_device():
    assert observed_risk_indicators(Decimal("100000"), GraphFeatures()) == []


def test_subcent_average_is_not_rounded_before_comparison():
    features = GraphFeatures(card_avg_amount_7d=Decimal("0.335"))
    assert observed_risk_indicators(Decimal("1.00"), features) == []
    assert observed_risk_indicators(Decimal("1.01"), features) == ["AMOUNT_AT_LEAST_3X_CARD_AVERAGE_7D"]


@pytest.mark.parametrize("amount,features", [
    (Decimal("10"), None), (Decimal("10"), {}),
    (Decimal("10"), GraphFeatures(card_txn_count_24h=True)),
    (Decimal("10"), GraphFeatures(card_avg_amount_7d=Decimal("NaN"))),
    (Decimal("NaN"), GraphFeatures()), (Decimal("-1"), GraphFeatures()),
])
def test_unavailable_or_malformed_inputs_cannot_create_signals(amount, features):
    with pytest.raises(ValueError):
        observed_risk_indicators(amount, features)


def test_order_is_stable_and_component_has_no_side_effects(monkeypatch):
    import socket
    from sqlalchemy.engine import Engine

    def forbidden(*args, **kwargs):
        pytest.fail("Indicators attempted I/O")

    monkeypatch.setattr(Engine, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    features = GraphFeatures(2, 2, 2, 2, Decimal("10"))
    first = observed_risk_indicators(Decimal("30"), features)
    assert first == observed_risk_indicators(Decimal("30"), features)
    assert len(first) == len(set(first)) == 5
