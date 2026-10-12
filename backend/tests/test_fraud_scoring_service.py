"""Offline orchestration tests: no model loading, training, SQL or AWS needed."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.graph.in_memory import InMemoryGraphRepository
from app.graph.models import GraphFeatures, TransactionNode
from app.ml.fraud_predictor import FraudPrediction, ModelError
from app.schemas.fraud_inference import TransactionModelAttributes
from app.schemas.fraud_scoring import FraudScoringRequest
from app.schemas.transaction import TransactionCreate
from app.services.fraud_inference_service import FraudInferenceService
from app.services.fraud_scoring_service import (
    FraudScoringError, FraudScoringService, GraphFreshness, GraphFreshnessError,
    GraphHistoryError,
)


TIME = datetime(2026, 10, 10, 5, tzinfo=timezone.utc)


def request(**changes):
    return FraudScoringRequest(**{
        "transaction_id": "txn-1", "amount": "25.00", "currency": "USD",
        "card_token": "card-pseudonym", "device_token": "device-pseudonym",
        "event_time": TIME, **changes,
    })


class Freshness:
    """Only test fixtures are known complete; this is not production attestation."""

    def __init__(self, status=GraphFreshness.FRESH):
        self.status = status
        self.requests = []
        self.active = False

    @contextmanager
    def guard(self, data):
        self.requests.append(data)
        self.active = True
        try:
            yield self.status
        finally:
            self.active = False


@pytest.fixture
def dependencies():
    freshness = Freshness()

    class Graph:
        calls = 0
        error = None
        features = GraphFeatures(
            card_txn_count_24h=2, device_txn_count_24h=1,
            card_unique_devices_7d=1, device_unique_cards_7d=1,
            card_avg_amount_7d=Decimal("20.123"),
        )

        def get_historical_features(self, *args):
            assert freshness.active
            self.calls += 1
            self.args = args
            if self.error:
                raise self.error
            return self.features

        def upsert_transaction(self, *args):
            pytest.fail("Scoring must not write to the graph")

    class Predictor:
        model_version = "test-model-v1"
        training_currency = "USD"
        threshold = 0.75
        calls = 0
        error = None

        def predict(self, rows):
            assert freshness.active
            self.calls += 1
            self.rows = rows
            if self.error:
                raise self.error
            return [FraudPrediction(0.75, True, self.threshold)]

    graph, predictor = Graph(), Predictor()
    inference = FraudInferenceService(predictor)
    service = FraudScoringService(graph, inference, freshness)
    return SimpleNamespace(graph=graph, predictor=predictor, inference=inference,
                           freshness=freshness, service=service)


def test_valid_input_utc_and_existing_api_unchanged():
    data = request(event_time="2026-10-10T10:30:00+05:30", ProductCD="W", M1="T")
    assert data.event_time == TIME and data.event_time.tzinfo is timezone.utc
    assert data.amount == Decimal("25") and data.M9 is None
    assert TransactionCreate(
        transaction_id="old", amount="1", card_token="card", device_token="",
        event_time=TIME,
    ).currency == "INR"


@pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
def test_empty_device_normalized(value):
    assert request(device_token=value).device_token is None


def test_omitted_device_and_categories():
    fields = request().model_dump(exclude={"device_token"})
    data = FraudScoringRequest(**fields)
    assert data.device_token is None
    assert all(getattr(data, name) is None for name in TransactionModelAttributes.model_fields)
    assert request(device_token=" device ").device_token == " device "


@pytest.mark.parametrize("changes", [
    {"currency": "INR"}, {"currency": "usd"}, {"amount": "0"},
    {"amount": "-1"}, {"amount": "NaN"}, {"amount": "1.234"},
    {"card_token": ""}, {"card_token": "   "}, {"card_token": 123},
    {"device_token": False}, {"event_time": "2026-10-10T05:00:00"},
    {"transaction_id": "bad id"}, {"isFraud": 1},
    {"ProductCD": True}, {"M1": 1}, {"M2": []}, {"M3": " "},
    {"M4": "__MISSING__"},
])
def test_malformed_inputs_rejected(changes):
    with pytest.raises(ValidationError):
        request(**changes)


def test_usd_is_required():
    with pytest.raises(ValidationError):
        FraudScoringRequest(**request().model_dump(exclude={"currency"}))


def test_feature_mapping_one_lookup_and_typed_metadata(dependencies):
    d = dependencies
    data = request(ProductCD="W", **{f"M{i}": "T" for i in range(1, 10)})
    result = d.service.score(data)
    assert d.graph.calls == d.predictor.calls == 1
    assert d.graph.args == (data.card_token, data.device_token, TIME)
    assert d.freshness.requests == [data] and not d.freshness.active
    row = d.predictor.rows[0]
    assert row == {
        "amount": Decimal("25"), "device_missing": 0,
        "card_txn_count_24h": 2, "device_txn_count_24h": 1,
        "card_unique_devices_7d": 1, "device_unique_cards_7d": 1,
        "card_avg_amount_7d": Decimal("20.123"),
        "attributes": {"ProductCD": "W", **{f"M{i}": "T" for i in range(1, 10)}},
    }
    assert result.transaction_id == data.transaction_id
    assert result.risk_score == 0.75 and result.risk_level == "HIGH"
    assert result.model_version == "test-model-v1"
    metadata = result.historical_features
    assert metadata.cutoff == TIME
    assert metadata.window_start_24h == TIME - timedelta(hours=24)
    assert metadata.window_start_7d == TIME - timedelta(days=7)
    assert metadata.boundary_policy == "[start, end)"
    assert metadata.freshness == "VERIFIED" and metadata.currency == "USD"
    assert metadata.card_avg_amount_7d == Decimal("20.123")
    assert "token" not in result.model_dump_json()


def test_absent_device_and_missing_categories_mapping(dependencies):
    d = dependencies
    d.graph.features = GraphFeatures()
    result = d.service.score(request(device_token=" "))
    assert d.predictor.rows[0]["device_missing"] == 1
    assert set(d.predictor.rows[0]["attributes"].values()) == {None}
    assert result.historical_features.card_txn_count_24h == 0


@pytest.mark.parametrize("status", [GraphFreshness.UNKNOWN, GraphFreshness.STALE, "FRESH", True])
def test_unverified_freshness_rejects_before_graph_and_model(dependencies, status):
    d = dependencies
    d.freshness.status = status
    with pytest.raises(GraphFreshnessError):
        d.service.score(request())
    assert d.graph.calls == d.predictor.calls == 0
    assert not d.freshness.active


def test_default_freshness_is_unknown(dependencies):
    d = dependencies
    service = FraudScoringService(d.graph, d.inference)
    with pytest.raises(GraphFreshnessError):
        service.score(request())
    assert d.graph.calls == d.predictor.calls == 0


@pytest.mark.parametrize("error", [RuntimeError("private-token"), ValueError("mixed currencies")])
def test_graph_failure_has_no_zero_fallback_or_retry(dependencies, error):
    d = dependencies
    d.graph.error = error
    with pytest.raises(GraphHistoryError, match="Historical graph lookup failed") as failure:
        d.service.score(request())
    assert "private-token" not in str(failure.value)
    assert d.graph.calls == 1 and d.predictor.calls == 0
    assert not d.freshness.active


@pytest.mark.parametrize("features", [None, {}, GraphFeatures(card_txn_count_24h=-1)])
def test_invalid_graph_response_cannot_produce_score(dependencies, features):
    d = dependencies
    d.graph.features = features
    with pytest.raises(GraphHistoryError):
        d.service.score(request())
    assert d.graph.calls == 1 and d.predictor.calls == 0


def test_inference_failure_propagates_without_fallback(dependencies):
    d = dependencies
    d.predictor.error = ModelError("Classifier unavailable")
    with pytest.raises(ModelError):
        d.service.score(request())
    assert d.graph.calls == d.predictor.calls == 1
    assert not d.freshness.active


@pytest.mark.parametrize("fail_on_exit", [False, True])
def test_freshness_checker_failure_never_returns_result(dependencies, fail_on_exit):
    d = dependencies

    class BrokenFreshness:
        @contextmanager
        def guard(self, data):
            if not fail_on_exit:
                raise RuntimeError("private-token")
            yield GraphFreshness.FRESH
            raise RuntimeError("private-token")

    service = FraudScoringService(d.graph, d.inference, BrokenFreshness())
    # Graph/predictor fakes check the normal guard, so mark it active for this test.
    d.freshness.active = True
    with pytest.raises(FraudScoringError, match="Fraud scoring failed"):
        service.score(request())
    assert d.graph.calls == int(fail_on_exit)


def test_copied_request_revalidated_before_dependencies(dependencies):
    d = dependencies
    with pytest.raises(FraudScoringError, match="Malformed"):
        d.service.score(request().model_copy(update={"currency": "INR"}))
    with pytest.raises(FraudScoringError, match="validated"):
        d.service.score({})
    assert d.freshness.requests == [] and d.graph.calls == 0


def test_real_adapter_cutoffs_and_no_current_insertion(dependencies):
    graph = InMemoryGraphRepository()
    for name, offset, amount in [
        ("lower7d", timedelta(days=-7), "10"),
        ("lower24h", timedelta(hours=-24), "30"),
        ("too_old", timedelta(days=-7, seconds=-1), "500"),
        ("exact", timedelta(0), "500"),
        ("future", timedelta(seconds=1), "500"),
    ]:
        graph.upsert_transaction(TransactionNode(
            transaction_id=name, amount=amount, currency="USD",
            card_token="card-pseudonym", device_token="device-pseudonym",
            event_time=TIME + offset,
        ))
    service = FraudScoringService(graph, dependencies.inference, dependencies.freshness)
    result = service.score(request())
    features = result.historical_features
    assert features.card_txn_count_24h == features.device_txn_count_24h == 1
    assert features.card_unique_devices_7d == features.device_unique_cards_7d == 1
    assert features.card_avg_amount_7d == Decimal("20")
    assert graph.get_transaction_network("txn-1").nodes == ()


def test_no_sql_graph_writes_or_network_connections(dependencies, monkeypatch):
    import socket
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session
    from app.graph.neptune import NeptuneGraphRepository

    def forbidden(*args, **kwargs):
        pytest.fail("Scoring attempted persistence or network access")

    for name in ("add", "commit", "execute", "flush"):
        monkeypatch.setattr(Session, name, forbidden)
    monkeypatch.setattr(Engine, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(InMemoryGraphRepository, "upsert_transaction", forbidden)
    monkeypatch.setattr(NeptuneGraphRepository, "upsert_transaction", forbidden)
    monkeypatch.setattr(NeptuneGraphRepository, "get_historical_features", forbidden)
    assert dependencies.service.score(request()).risk_level == "HIGH"
