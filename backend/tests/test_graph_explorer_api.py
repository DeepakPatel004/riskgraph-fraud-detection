"""Read-only explorer integration tests: temporary SQL and offline graph adapters."""

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.api.graph_dependencies import get_graph_population_factory
from app.core.config import Settings
from app.core.database import get_db
from app.graph.models import CardNode, GraphNetwork, GraphRelationship, NodeRef, TransactionNode
from app.graph.neptune_client import NeptuneTransportError
from app.main import app
from app.models import FraudAlert, GraphSyncJob, IdempotencyRecord, Transaction
from app.services.graph_population import GraphPopulation, neptune_population_id
from tests.fake_neptune import FakeNeptune, fake_repository
from tests.test_scoring_api import TIME, environment, payload


ROOT = "/api/v1/transactions"
POPULATION = "fixture-explorer-population"
CARD = "private-card-token-4111111111111111"
DEVICE = "private-device-token-credential"


def url(name="T1"):
    return f"{ROOT}/{name}/graph"


def insert(e, name="T1", card=CARD, device=DEVICE, *, status="SYNCED", job_status="SYNCED",
           population=POPULATION, lease=None, graph=True):
    node = TransactionNode(transaction_id=name, amount="150.00", currency="USD",
                           card_token=card, device_token=device, event_time=TIME)
    with e.sessions() as db:
        db.add(Transaction(**node.model_dump(), graph_sync_status=status))
        db.flush()
        if job_status is not None:
            db.add(GraphSyncJob(transaction_id=name, status=job_status,
                                graph_population_id=population, lease_token=lease))
        db.commit()
    if graph:
        e.graph.upsert_transaction(node)
    return node


def sql_snapshot(e):
    with e.sessions() as db:
        return [db.execute(select(model.__table__).order_by(*model.__table__.primary_key)).all()
                for model in (Transaction, GraphSyncJob, IdempotencyRecord, FraudAlert)]


@pytest.fixture
def explorer(environment):
    e = environment
    active_sessions = []

    def override_db():
        with e.sessions() as db:
            active_sessions.append(db)
            try:
                yield db
            finally:
                active_sessions.remove(db)

    class ReadOnlyGraph:
        calls = 0
        writes = 0
        error = None
        replacement = None
        hook = None

        def get_transaction_network(self, transaction_id, limit=50):
            assert all(not db.in_transaction() for db in active_sessions)
            self.calls += 1
            self.args = (transaction_id, limit)
            if self.hook:
                self.hook()
            if self.error:
                raise self.error
            if self.replacement is not None:
                return self.replacement
            return e.graph.get_transaction_network(transaction_id, limit)

        def upsert_transaction(self, transaction):
            self.writes += 1
            pytest.fail("Explorer must not write to the graph")

        def get_historical_features(self, *args):
            pytest.fail("Explorer must not calculate scoring features")

    e.reader = ReadOnlyGraph()
    e.population_id, e.population_verified = POPULATION, True
    e.population_error, e.population_calls = None, 0

    def factory(engine):
        assert engine is e.engine
        assert all(not db.in_transaction() for db in active_sessions)
        e.population_calls += 1
        if e.population_error:
            raise e.population_error
        return GraphPopulation(e.reader, e.population_id, verified=e.population_verified)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_graph_population_factory] = lambda: factory
    return e


def assert_safe(result):
    assert CARD not in result.text and DEVICE not in result.text
    assert "4111111111111111" not in result.text


def test_network_contains_shared_card_device_and_only_allowed_fields(explorer):
    e = explorer
    insert(e)
    insert(e, "T2", device="other-private-device")
    insert(e, "T3", card="other-private-card")
    insert(e, "UNRELATED", card="unrelated", device="unrelated")
    before = sql_snapshot(e)
    first = e.client.get(url())
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["transaction_id"] == "T1"
    assert data["node_count"] == len(data["nodes"]) == 7
    assert data["edge_count"] == len(data["edges"]) == 6 and data["truncated"] is False
    transactions = [node for node in data["nodes"] if node["type"] == "Transaction"]
    assert {node["label"] for node in transactions} == {"T1", "T2", "T3"}
    assert all(node["properties"] == {"amount": "150.00", "currency": "USD"} for node in transactions)
    ids = {node["id"] for node in data["nodes"]}
    assert len(ids) == 7 and all(edge["source"] in ids and edge["target"] in ids for edge in data["edges"])
    assert {edge["type"] for edge in data["edges"]} == {"USES_CARD", "USES_DEVICE"}
    assert all(node["properties"] == {} for node in data["nodes"] if node["type"] != "Transaction")
    assert all(set(node) == {"id", "type", "label", "properties"} for node in data["nodes"])
    assert e.reader.args == ("T1", 50)
    assert e.client.get(url()).json() == data  # Stable ordering and IDs across reads.
    assert sql_snapshot(e) == before and e.reader.writes == e.predictor.calls == 0
    assert_safe(first)
    assert "other-private" not in first.text and "risk_score" not in first.text


def test_missing_device_has_card_only_network(explorer):
    e = explorer
    insert(e, device=None)
    result = e.client.get(url())
    assert result.status_code == 200
    data = result.json()
    assert [node["type"] for node in data["nodes"]] == ["Transaction", "Card"]
    assert data["node_count"] == 2 and data["edge_count"] == 1 and data["truncated"] is False
    assert data["edges"][0]["type"] == "USES_CARD"
    assert_safe(result)


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 50, 100])
def test_limit_counts_all_nodes_and_does_not_invent_truncation(explorer, limit):
    e = explorer
    insert(e)
    result = e.client.get(url(), params={"limit": limit})
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["node_count"] == min(limit, 3)
    assert data["truncated"] == (False if limit > 3 else None)
    assert e.reader.args == ("T1", limit)
    ids = {node["id"] for node in data["nodes"]}
    assert all(edge["source"] in ids and edge["target"] in ids for edge in data["edges"])
    assert data["edge_count"] == len(data["edges"])


def test_large_shared_card_component_is_bounded_by_default_and_maximum(explorer):
    e = explorer
    for i in range(110):
        insert(e, f"T{i:03}", device=None)
    default = e.client.get(url("T000")).json()
    maximum = e.client.get(url("T000"), params={"limit": 100}).json()
    assert default["node_count"] == 50 and maximum["node_count"] == 100
    assert default["truncated"] is None and maximum["truncated"] is None
    assert default["edge_count"] == 49 and maximum["edge_count"] == 99


@pytest.mark.parametrize("limit", [0, -1, 101, "1.5", "private-device-token-credential"])
def test_invalid_limits_are_structured_422_before_any_graph_work(explorer, limit):
    e = explorer
    result = e.client.get(url(), params={"limit": limit})
    assert result.status_code == 422
    assert result.json()["error"] == {"code": "VALIDATION_ERROR", "retryable": False}
    assert e.reader.calls == e.population_calls == 0
    assert_safe(result)


def test_unknown_sql_transaction_is_404_without_dependency_construction(explorer):
    e = explorer
    result = e.client.get(url())
    assert result.status_code == 404
    assert result.json() == {"detail": "Transaction not found",
                             "error": {"code": "TRANSACTION_NOT_FOUND", "retryable": False}}
    assert e.reader.calls == e.population_calls == 0


@pytest.mark.parametrize("status,code,retryable", [
    ("PENDING", "GRAPH_SYNC_PENDING", True), ("PROCESSING", "GRAPH_SYNC_PENDING", True),
    ("FAILED", "GRAPH_SYNC_FAILED", False), ("UNKNOWN", "GRAPH_SYNC_UNVERIFIED", False),
])
def test_unsynchronized_sql_state_is_unavailable_before_graph_construction(explorer, status, code, retryable):
    e = explorer
    insert(e, status=status, job_status=status)
    result = e.client.get(url())
    assert result.status_code == 503
    assert result.json()["error"] == {"code": code, "retryable": retryable}
    if retryable:
        assert result.headers["Retry-After"] == "2"
    assert e.reader.calls == e.population_calls == 0


@pytest.mark.parametrize("changes,code", [
    ({"job_status": None}, "GRAPH_SYNC_UNVERIFIED"),
    ({"job_status": "PROCESSING"}, "GRAPH_SYNC_UNVERIFIED"),
    ({"population": None}, "GRAPH_SYNC_UNVERIFIED"),
    ({"population": "another-graph"}, "GRAPH_POPULATION_MISMATCH"),
    ({"lease": "private-lease-token"}, "GRAPH_SYNC_UNVERIFIED"),
])
def test_synced_requires_complete_matching_job_acknowledgement(explorer, changes, code):
    e = explorer
    insert(e, **changes)
    result = e.client.get(url())
    assert result.status_code == 503 and result.json()["error"]["code"] == code
    assert e.reader.calls == 0
    assert "private-lease-token" not in result.text


def test_unverified_injected_population_is_rejected(explorer):
    e = explorer
    insert(e)
    e.population_verified = False
    result = e.client.get(url())
    assert result.status_code == 503 and result.json()["error"]["code"] == "GRAPH_POPULATION_UNVERIFIED"
    assert e.reader.calls == 0


def test_synced_but_absent_graph_seed_is_inconsistency_not_empty_success(explorer):
    e = explorer
    insert(e, graph=False)
    result = e.client.get(url())
    assert result.status_code == 503 and result.json()["error"]["code"] == "GRAPH_INCONSISTENT"


@pytest.mark.parametrize("problem", ["wrong-seed", "missing-card", "dangling-edge", "disconnected", "over-limit", "wrong-amount"])
def test_malformed_or_inconsistent_network_is_not_returned_as_success(explorer, problem):
    e = explorer
    node = insert(e)
    valid = e.graph.get_transaction_network("T1")
    if problem == "wrong-seed":
        e.reader.replacement = GraphNetwork((node.model_copy(update={"transaction_id": "other"}),))
    elif problem == "missing-card":
        e.reader.replacement = GraphNetwork((node,))
    elif problem == "dangling-edge":
        e.reader.replacement = GraphNetwork(valid.nodes, valid.relationships + (
            GraphRelationship(node.ref, NodeRef("Card", "absent"), "USES_CARD"),))
    elif problem == "disconnected":
        e.reader.replacement = GraphNetwork(valid.nodes + (CardNode("private-disconnected"),), valid.relationships)
    elif problem == "wrong-amount":
        e.graph.upsert_transaction(node.model_copy(update={"amount": node.amount + 1}))
    else:
        e.reader.replacement = valid
    result = e.client.get(url(), params={"limit": 1 if problem == "over-limit" else 50})
    assert result.status_code == 503 and result.json()["error"]["code"] == "GRAPH_INCONSISTENT"
    assert_safe(result)


def test_returned_neighbor_requires_sql_source_and_matching_population(explorer):
    e = explorer
    insert(e)
    insert(e, "neighbor", population="another-graph")
    result = e.client.get(url())
    assert result.status_code == 503 and result.json()["error"]["code"] == "GRAPH_POPULATION_MISMATCH"


def test_foreign_transaction_in_graph_is_not_exposed(explorer):
    e = explorer
    insert(e)
    node = TransactionNode(transaction_id="foreign-private-transaction", amount="150.00",
                           currency="USD", card_token=CARD, device_token=None, event_time=TIME)
    e.graph.upsert_transaction(node)
    result = e.client.get(url())
    assert result.status_code == 503 and "foreign-private-transaction" not in result.text


def test_sync_state_change_during_read_is_detected(explorer):
    e = explorer
    insert(e)

    def change_state():
        with e.sessions() as db:
            db.get(Transaction, "T1").graph_sync_status = "PROCESSING"
            db.commit()

    e.reader.hook = change_state
    result = e.client.get(url())
    assert result.status_code == 503 and result.json()["error"]["code"] == "GRAPH_SYNC_PENDING"


@pytest.mark.parametrize("error", [NeptuneTransportError("private AWS credentials " + CARD),
                                   TimeoutError(DEVICE), ConnectionError(CARD)])
def test_neptune_and_network_failures_are_retryable_sanitized_503(explorer, error, caplog):
    e = explorer
    insert(e)
    before = sql_snapshot(e)
    e.reader.error = error
    result = e.client.get(url())
    assert result.status_code == 503
    assert result.json()["error"] == {"code": "GRAPH_UNAVAILABLE", "retryable": True}
    assert sql_snapshot(e) == before and e.reader.writes == 0
    assert_safe(result)
    assert CARD not in caplog.text and DEVICE not in caplog.text


@pytest.mark.parametrize("failure", ["factory", "graph", "sql", "http-exception"])
def test_unexpected_errors_are_sanitized_and_structured(explorer, monkeypatch, failure, caplog):
    from fastapi import HTTPException
    e = explorer
    insert(e)
    if failure == "factory":
        e.population_error = RuntimeError(CARD)
    elif failure == "http-exception":
        e.population_error = HTTPException(500, CARD)
    elif failure == "graph":
        e.reader.error = RuntimeError(DEVICE)
    else:
        def fail(*args, **kwargs):
            raise OperationalError(CARD, {}, RuntimeError(DEVICE))
        monkeypatch.setattr(e.sessions.class_, "execute", fail)
    result = e.client.get(url())
    assert result.status_code == 500
    assert result.json()["error"] == {"code": "GRAPH_EXPLORER_FAILED", "retryable": False}
    assert_safe(result)
    assert CARD not in caplog.text and DEVICE not in caplog.text


@pytest.mark.parametrize("changes", [
    {}, {"graph_sync_adapter": "memory"}, {"graph_population_verified": False},
    {"graph_population_name": None}, {"scoring_api_workers": 2},
])
def test_production_missing_or_unverified_configuration_refuses_graph(explorer, monkeypatch, changes):
    e = explorer
    insert(e)
    app.dependency_overrides.pop(get_graph_population_factory)
    values = dict(graph_sync_adapter="neptune", graph_population_verified=True,
                  graph_population_name="demo", neptune_endpoint="neptune.invalid")
    if not changes:
        values["neptune_endpoint"] = None
    values.update(changes)
    monkeypatch.setattr("app.core.config.get_settings", lambda: Settings(_env_file=None, **values))

    def forbidden(*args):
        pytest.fail("Unsafe configuration must not construct a graph client")

    monkeypatch.setattr("app.api.scoring_dependencies._graph", forbidden)
    result = e.client.get(url())
    assert result.status_code == 503 and result.json()["error"]["code"] == "GRAPH_POPULATION_UNVERIFIED"


def test_configured_factory_uses_existing_population_identity_without_model_or_scoring_enablement(explorer, monkeypatch):
    e = explorer
    settings = Settings(_env_file=None, graph_sync_adapter="neptune", graph_population_verified=True,
                        graph_population_name="verified-demo", neptune_endpoint="neptune.invalid",
                        fraud_scoring_enabled=False, neptune_iam_auth_enabled=False)
    identity = neptune_population_id(settings, e.engine)
    insert(e, population=identity)
    app.dependency_overrides.pop(get_graph_population_factory)
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    monkeypatch.setattr("app.api.scoring_dependencies._graph", lambda *args: e.reader)
    result = e.client.get(url())
    assert result.status_code == 200 and e.predictor.calls == 0


def test_invalid_neptune_configuration_fails_without_network(explorer, monkeypatch):
    e = explorer
    settings = Settings(_env_file=None, graph_sync_adapter="neptune", graph_population_verified=True,
                        graph_population_name="verified-demo", neptune_endpoint="private.invalid/path",
                        neptune_iam_auth_enabled=False)
    insert(e, population=neptune_population_id(settings, e.engine))
    app.dependency_overrides.pop(get_graph_population_factory)
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    result = e.client.get(url())
    assert result.status_code == 503 and result.json()["error"]["code"] == "GRAPH_CONFIGURATION_UNAVAILABLE"
    assert "private.invalid" not in result.text


def test_actual_neptune_adapter_over_mock_http_preserves_read_only_mapping(explorer):
    e = explorer
    node = insert(e)
    server = FakeNeptune()
    with fake_repository(server) as adapter:
        adapter.upsert_transaction(node)
        server.calls.clear()
        e.graph = adapter
        result = e.client.get(url())
        assert result.status_code == 200 and result.json()["node_count"] == 3
        assert all(query.startswith(("// riskgraph:seed", "// riskgraph:neighbors", "// riskgraph:edges"))
                   for query, _ in server.calls)
        assert all("MERGE" not in query and "SET " not in query and "DELETE" not in query for query, _ in server.calls)
        assert_safe(result)


def test_existing_read_scoring_alert_and_health_routes_remain_independent(explorer):
    e = explorer
    insert(e)
    assert e.client.get(f"{ROOT}/T1").status_code == 200
    assert e.client.get(ROOT).json()["total"] == 1
    assert e.client.get("/api/v1/alerts").json()["items"] == []
    assert e.client.get("/health").status_code == 200
    request = payload("new-score", event_time=(TIME + timedelta(seconds=1)).isoformat())
    # Unrelated explorer tokens are not history for this scoring request.
    result = e.client.post(ROOT + "/score", json=request)
    assert result.status_code == 201 and result.json()["risk_level"] == "HIGH"
    assert e.client.get("/api/v1/alerts").json()["total"] == 1
    assert e.reader.calls == e.population_calls == 0


def test_openapi_has_bounded_limit_nullable_truncation_and_error_contract(explorer):
    document = explorer.client.get("/openapi.json").json()
    operation = document["paths"][ROOT + "/{transaction_id}/graph"]["get"]
    limit = next(p["schema"] for p in operation["parameters"] if p["name"] == "limit")
    assert (limit["minimum"], limit["maximum"], limit["default"]) == (1, 100, 50)
    for code in ("404", "422", "503", "500"):
        assert operation["responses"][code]["content"]["application/json"]["schema"]["$ref"].endswith("/ScoringErrorResponse")
    truncated = document["components"]["schemas"]["GraphExplorerResponse"]["properties"]["truncated"]
    assert {item["type"] for item in truncated["anyOf"]} == {"boolean", "null"}
