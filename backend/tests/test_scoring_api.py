"""Public workflow tests with file SQLite, real SQL freshness and fake inference."""

import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.api.scoring_dependencies import build_scoring_service, get_scoring_factory
from app.core.config import Settings
from app.core.database import Base, get_db, make_engine
from app.graph.in_memory import InMemoryGraphRepository
from app.graph.models import TransactionNode
from app.main import app
from app.ml.fraud_predictor import FraudPrediction, ModelError
from app.models import GraphSyncJob, IdempotencyRecord, Transaction
from app.services.fraud_inference_service import FraudInferenceService
from app.services.fraud_scoring_service import FraudScoringService
from app.services.graph_population import GraphPopulation, neptune_population_id
from app.services.graph_sync_service import GraphSyncService
from app.services.ingestion_gate import IngestionGate, ingestion_gate
from app.services.sql_graph_freshness import SQLGraphFreshnessChecker
from scripts.migrate_graph_population import upgrade_graph_population


TIME = datetime(2026, 10, 10, 5, tzinfo=timezone.utc)
URL = "/api/v1/transactions/score"


def payload(name="scored-1", **changes):
    return {"transaction_id": name, "amount": "100.00", "currency": "USD",
            "card_token": "card-pseudonym", "device_token": "device-pseudonym",
            "event_time": TIME.isoformat(), "ProductCD": "W", **changes}


@pytest.fixture
def environment():
    parent = Path(__file__).resolve().parents[2] / "data/test-scoring-api"
    root = parent / uuid4().hex
    root.mkdir(parents=True)
    engine = make_engine(f"sqlite:///{(root / 'source.db').as_posix()}")
    Base.metadata.create_all(engine)
    sessions = []

    class TrackedSession(Session):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            sessions.append(self)

    session_factory = sessionmaker(bind=engine, class_=TrackedSession, expire_on_commit=False)

    class Graph(InMemoryGraphRepository):
        calls = 0
        error = None
        hook = None

        def get_historical_features(self, *args):
            assert not any(db.in_transaction() for db in sessions)
            self.calls += 1
            self.args = args
            if self.hook:
                self.hook()
            if self.error:
                raise self.error
            return super().get_historical_features(*args)

    class Predictor:
        model_version = "fixture-version"
        training_currency = "USD"
        threshold = 0.75
        calls = 0
        score = 0.8

        def predict(self, rows):
            assert not any(db.in_transaction() for db in sessions)
            self.calls += 1
            self.rows = rows
            return [FraudPrediction(self.score, self.score >= self.threshold, self.threshold)]

    graph, predictor = Graph(), Predictor()
    population = GraphPopulation(graph, "fixture-population", verified=True)
    state = SimpleNamespace(root=root, engine=engine, sessions=session_factory,
                            graph=graph, predictor=predictor, population=population,
                            factory_error=None, verified=True)

    def factory(bind, gate):
        assert bind is engine
        if state.factory_error:
            raise state.factory_error
        selected = GraphPopulation(graph, population.identifier, verified=state.verified)
        return FraudScoringService(graph, FraudInferenceService(predictor),
                                   SQLGraphFreshnessChecker(session_factory, selected, gate))

    def override_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_scoring_factory] = lambda: factory
    try:
        with TestClient(app) as client:
            state.client = client
            yield state
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
        assert root.resolve().parent == parent.resolve()
        shutil.rmtree(root)


def counts(e):
    with e.sessions() as db:
        return tuple(db.scalar(select(func.count()).select_from(model))
                     for model in (Transaction, GraphSyncJob, IdempotencyRecord))


def seed(e, *, status="SYNCED", job_status="SYNCED", population="fixture-population",
         offset=timedelta(hours=-1), card="card-pseudonym", device="device-pseudonym",
         currency="USD", lease=None):
    node = TransactionNode(transaction_id="prior", amount="20", currency=currency,
                           card_token=card, device_token=device, event_time=TIME + offset)
    with e.sessions() as db:
        db.add(Transaction(**node.model_dump(), graph_sync_status=status))
        db.flush()
        if job_status is not None:
            db.add(GraphSyncJob(transaction_id="prior", status=job_status,
                                graph_population_id=population, lease_token=lease))
        db.commit()
    if job_status == "SYNCED":
        e.graph.upsert_transaction(node)


@pytest.mark.parametrize("score,level", [(0.8, "HIGH"), (0.2, "LOW"), (0.75, "HIGH")])
def test_score_persistence_cutoff_and_one_job(environment, score, level):
    e = environment
    seed(e)
    e.predictor.score = score
    response = e.client.post(URL, json=payload(), headers={"Idempotency-Key": "score-key"})
    assert response.status_code == 201, response.text
    assert response.json() == {"transaction_id": "scored-1", "risk_score": score,
                               "risk_level": level, "model_version": "fixture-version",
                               "graph_sync_status": "PENDING"}
    assert e.graph.calls == e.predictor.calls == 1
    assert e.graph.args == ("card-pseudonym", "device-pseudonym", TIME)
    assert e.graph.get_transaction_network("scored-1").nodes == ()
    with e.sessions() as db:
        saved = db.get(Transaction, "scored-1")
        assert saved.risk_score == score and saved.processing_status == "SCORED"
        metadata = saved.features_json["fraud_scoring"]
        assert metadata["model_version"] == "fixture-version"
        assert metadata["attributes"]["ProductCD"] == "W"
        assert metadata["attributes"]["M9"] is None
        assert metadata["historical_features"]["card_txn_count_24h"] == 1
        assert metadata["historical_features"]["cutoff"] == "2026-10-10T05:00:00Z"
        assert db.scalar(select(func.count()).select_from(GraphSyncJob).where(
            GraphSyncJob.transaction_id == "scored-1")) == 1


def test_replay_skips_factory_and_returns_current_sync_state(environment):
    e = environment
    headers = {"Idempotency-Key": "same"}
    first = e.client.post(URL, json=payload(), headers=headers)
    assert first.status_code == 201
    with e.sessions() as db:
        db.get(Transaction, "scored-1").graph_sync_status = "SYNCED"
        db.commit()
    e.factory_error = ModelError("model offline")
    data = payload(event_time="2026-10-10T10:30:00+05:30")
    replay = e.client.post(URL, json=data, headers=headers)
    assert replay.status_code == 200
    assert replay.json()["risk_score"] == first.json()["risk_score"]
    assert replay.json()["graph_sync_status"] == "SYNCED"
    assert e.graph.calls == e.predictor.calls == 1
    assert counts(e) == (1, 1, 1)


@pytest.mark.parametrize("changes", [{"amount": "101"}, {"M1": "T"}, {"transaction_id": "different"}])
def test_conflicting_key_reuse(environment, changes):
    e = environment
    headers = {"Idempotency-Key": "same"}
    assert e.client.post(URL, json=payload(), headers=headers).status_code == 201
    assert e.client.post(URL, json=payload(**changes), headers=headers).status_code == 409
    assert counts(e) == (1, 1, 1)
    assert e.graph.calls == 1


def test_duplicate_id_and_cross_endpoint_key_conflict(environment):
    e = environment
    assert e.client.post(URL, json=payload(), headers={"Idempotency-Key": "same"}).status_code == 201
    assert e.client.post(URL, json=payload()).status_code == 409
    legacy = payload("legacy")
    legacy.pop("ProductCD")
    assert e.client.post("/api/v1/transactions", json=legacy,
                         headers={"Idempotency-Key": "same"}).status_code == 409
    assert counts(e) == (1, 1, 1)


@pytest.mark.parametrize("changes", [{"currency": "INR"}, {"M1": True},
                                     {"card_token": " "}, {"event_time": "2026-10-10T05:00:00"}])
def test_invalid_input_422_without_work(environment, changes):
    e = environment
    assert e.client.post(URL, json=payload(**changes)).status_code == 422
    assert counts(e) == (0, 0, 0) and e.graph.calls == 0


def test_blank_device_and_null_categories(environment):
    e = environment
    assert e.client.post(URL, json=payload(device_token=" ", ProductCD=None)).status_code == 201
    assert e.predictor.rows[0]["device_missing"] == 1
    assert set(e.predictor.rows[0]["attributes"].values()) == {None}
    with e.sessions() as db:
        assert db.get(Transaction, "scored-1").device_token is None


@pytest.mark.parametrize("status,job_status,population,lease", [
    ("PENDING", "PENDING", None, None),
    ("PROCESSING", "PROCESSING", None, "lease"),
    ("FAILED", "FAILED", None, None),
    ("SYNCED", None, None, None),
    ("SYNCED", "SYNCED", None, None),
    ("SYNCED", "SYNCED", "other-population", None),
    ("PENDING", "SYNCED", "fixture-population", None),
    ("SYNCED", "SYNCED", "fixture-population", "unexpected-lease"),
])
def test_incomplete_source_history_blocks(environment, status, job_status, population, lease):
    e = environment
    seed(e, status=status, job_status=job_status, population=population, lease=lease)
    before = counts(e)
    response = e.client.post(URL, json=payload(), headers={"Idempotency-Key": "retryable"})
    assert response.status_code == 503
    assert counts(e) == before and e.graph.calls == e.predictor.calls == 0


def test_unknown_population_empty_graph_not_proof(environment):
    e = environment
    e.verified = False
    assert e.client.post(URL, json=payload()).status_code == 503
    assert counts(e) == (0, 0, 0) and e.graph.calls == 0


def test_other_card_same_device_pending_blocks(environment):
    e = environment
    seed(e, card="different-card", status="PENDING", job_status="PENDING")
    assert e.client.post(URL, json=payload()).status_code == 503
    assert e.graph.calls == 0


def test_single_currency_inr_history_is_not_usd(environment):
    e = environment
    seed(e, currency="INR")
    assert e.client.post(URL, json=payload()).status_code == 503
    assert e.graph.calls == 0


def test_exact_time_is_excluded_even_if_pending(environment):
    e = environment
    seed(e, offset=timedelta(0), status="PENDING", job_status="PENDING")
    assert e.client.post(URL, json=payload()).status_code == 201
    assert e.predictor.rows[0]["card_txn_count_24h"] == 0


def test_late_scoring_and_legacy_events_rejected_but_retry_allowed(environment):
    e = environment
    assert e.client.post(URL, json=payload(), headers={"Idempotency-Key": "same"}).status_code == 201
    late = payload("late", event_time=(TIME - timedelta(seconds=1)).isoformat())
    assert e.client.post(URL, json=late).status_code == 409
    late.pop("ProductCD")
    assert e.client.post("/api/v1/transactions", json=late).status_code == 409
    assert e.client.post(URL, json=payload(), headers={"Idempotency-Key": "same"}).status_code == 200
    assert counts(e) == (1, 1, 1)


def test_legacy_api_backward_compatible_before_scoring(environment):
    e = environment
    legacy = payload("legacy")
    legacy.pop("ProductCD")
    legacy.pop("currency")
    assert e.client.post("/api/v1/transactions", json=legacy).status_code == 201
    assert e.client.get("/api/v1/transactions/legacy").json()["currency"] == "INR"
    assert e.client.get("/api/v1/transactions").json()["total"] == 1


@pytest.mark.parametrize("kind", ["graph", "model", "unexpected"])
def test_dependency_failures_do_not_consume_key(environment, kind):
    e = environment
    if kind == "graph":
        e.graph.error = RuntimeError("private-card-and-credentials")
    else:
        e.factory_error = (ModelError("private-card-and-credentials") if kind == "model"
                           else RuntimeError("private-card-and-credentials"))
    headers = {"Idempotency-Key": "retryable"}
    result = e.client.post(URL, json=payload(), headers=headers)
    assert result.status_code == (500 if kind == "unexpected" else 503)
    assert "private" not in result.text
    assert counts(e) == (0, 0, 0)
    e.graph.error = e.factory_error = None
    assert e.client.post(URL, json=payload(), headers=headers).status_code == 201


@pytest.mark.parametrize("stage", ["parent", "job", "idempotency"])
def test_integrity_failure_rolls_back_every_record(environment, monkeypatch, stage):
    e = environment
    cls = e.sessions.class_
    original = cls.flush

    def fail(db, *args, **kwargs):
        models = {type(row) for row in db.new}
        expected = {"parent": Transaction, "job": GraphSyncJob, "idempotency": IdempotencyRecord}[stage]
        if expected in models:
            raise IntegrityError("private SQL", {}, RuntimeError("private credentials"))
        return original(db, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(cls, "flush", fail)
        result = e.client.post(URL, json=payload(), headers={"Idempotency-Key": "retryable"})
        assert result.status_code == 500 and "private" not in result.text
        assert counts(e) == (0, 0, 0)
    assert e.client.post(URL, json=payload(), headers={"Idempotency-Key": "retryable"}).status_code == 201
    assert counts(e) == (1, 1, 1)


def test_concurrent_submission_is_busy_then_waits_for_outbox(environment):
    e = environment
    entered, release = Event(), Event()

    def block():
        entered.set()
        assert release.wait(10)

    e.graph.hook = block
    later = payload("later", event_time=(TIME + timedelta(seconds=1)).isoformat())
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(e.client.post, URL, json=payload())
        assert entered.wait(10)
        try:
            assert e.client.post(URL, json=later).status_code == 503
            legacy = payload("legacy")
            legacy.pop("ProductCD")
            assert e.client.post("/api/v1/transactions", json=legacy).status_code == 503
        finally:
            release.set()
        assert first.result().status_code == 201
    e.graph.hook = None
    assert e.client.post(URL, json=later).status_code == 503  # Earlier job still pending.
    assert counts(e) == (1, 1, 0) and e.graph.calls == 1


def test_worker_acknowledges_exact_population_before_next_score(environment):
    e = environment
    assert e.client.post(URL, json=payload()).status_code == 201
    worker = GraphSyncService(e.sessions, e.graph, clock=lambda: TIME + timedelta(days=10),
                              population_id=e.population.identifier)
    assert worker.process_batch() == 1
    with e.sessions() as db:
        job = db.scalar(select(GraphSyncJob))
        assert job.status == "SYNCED" and job.graph_population_id == e.population.identifier
    assert e.client.post(URL, json=payload("later", event_time=(TIME + timedelta(seconds=1)).isoformat())).status_code == 201
    assert e.predictor.rows[0]["card_txn_count_24h"] == 1


def test_sql_checker_requires_ingestion_gate(environment):
    e = environment
    checker = SQLGraphFreshnessChecker(e.sessions, e.population, ingestion_gate(e.engine))
    from app.schemas.fraud_scoring import FraudScoringRequest
    from app.services.fraud_scoring_service import GraphFreshness
    with checker.guard(FraudScoringRequest(**payload())) as status:
        assert status is GraphFreshness.UNKNOWN


def test_os_lock_excludes_other_processes(environment):
    path = environment.root / "cross-process.ingestion.lock"
    code = "from pathlib import Path; from app.services.ingestion_gate import IngestionGate; import sys;\nwith IngestionGate(Path(sys.argv[1])).hold(): pass"
    with IngestionGate(path).hold():
        result = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True)
        assert result.returncode != 0 and "IngestionBusy" in result.stderr
    result = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("settings", [Settings(fraud_scoring_enabled=False), Settings(fraud_scoring_enabled=True,
    graph_population_verified=True, graph_population_name="epoch", graph_sync_adapter="memory"),
    Settings(fraud_scoring_enabled=True, graph_population_verified=True, graph_population_name="epoch",
             graph_sync_adapter="neptune", neptune_endpoint="fake.invalid", scoring_api_workers=2)])
def test_production_factory_refuses_unsafe_configuration(environment, monkeypatch, settings):
    from fastapi import HTTPException
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    with pytest.raises(HTTPException) as error:
        build_scoring_service(environment.engine, ingestion_gate(environment.engine))
    assert error.value.status_code == 503


def test_population_identity_changes_with_source_endpoint_and_epoch(environment):
    e = environment
    settings = Settings(graph_sync_adapter="neptune", neptune_endpoint="fake.invalid", graph_population_name="epoch")
    first = neptune_population_id(settings, e.engine)
    assert first and first == neptune_population_id(settings, e.engine)
    assert first != neptune_population_id(settings.model_copy(update={"graph_population_name": "replacement"}), e.engine)
    assert first != neptune_population_id(settings.model_copy(update={"neptune_endpoint": "other.invalid"}), e.engine)


def test_additive_migration_preserves_rows_and_is_repeatable(environment):
    engine = make_engine(f"sqlite:///{(environment.root / 'old.db').as_posix()}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE graph_sync_jobs (job_id VARCHAR(36), status VARCHAR(16))")
            connection.exec_driver_sql("INSERT INTO graph_sync_jobs VALUES ('old-job', 'SYNCED')")
        assert upgrade_graph_population(engine)
        assert not upgrade_graph_population(engine)
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT * FROM graph_sync_jobs").all() == [("old-job", "SYNCED", None)]
    finally:
        engine.dispose()


def test_score_route_and_openapi_do_not_shadow_retrieval(environment):
    e = environment
    operation = e.client.get("/openapi.json").json()["paths"][URL]["post"]
    assert "503" in operation["responses"] and operation["description"]
    assert e.client.post(URL, json=payload()).status_code == 201
    assert e.client.get("/api/v1/transactions/scored-1").json()["risk_score"] == 0.8


def test_default_factory_fails_closed_without_aws_or_model_loading(environment, monkeypatch):
    import socket
    e = environment
    app.dependency_overrides.pop(get_scoring_factory)
    monkeypatch.setattr("app.core.config.get_settings", lambda: Settings(fraud_scoring_enabled=False))

    def forbidden(*args, **kwargs):
        pytest.fail("Disabled scoring tried loading artifacts or using the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("app.services.fraud_inference_service.get_fraud_inference_service", forbidden)
    assert e.client.post(URL, json=payload()).status_code == 503
    assert counts(e) == (0, 0, 0)


def test_validation_does_not_echo_tokens(environment):
    secret = "private-card-token-" * 20
    result = environment.client.post(URL, json=payload(card_token=secret))
    assert result.status_code == 422 and secret not in result.text
    assert "input" not in result.json()["detail"][0]


def test_database_freshness_failure_is_500_not_duplicate_or_success(environment, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def fail(*args, **kwargs):
        raise OperationalError("private SQL", {}, RuntimeError("secret"))

    monkeypatch.setattr(SQLGraphFreshnessChecker, "guard", fail)
    result = environment.client.post(URL, json=payload(), headers={"Idempotency-Key": "retryable"})
    assert result.status_code == 500 and "secret" not in result.text
    assert counts(environment) == (0, 0, 0)


def test_earlier_pending_event_blocks_despite_newer_synced_watermark(environment):
    e = environment
    seed(e, status="PENDING", job_status="PENDING")
    with e.sessions() as db:
        db.add(Transaction(transaction_id="newer", amount="1", currency="USD",
                           card_token="card-pseudonym", event_time=TIME - timedelta(minutes=1),
                           graph_sync_status="SYNCED"))
        db.flush()
        db.add(GraphSyncJob(transaction_id="newer", status="SYNCED",
                            graph_population_id=e.population.identifier))
        db.commit()
    assert e.client.post(URL, json=payload()).status_code == 503
    assert e.graph.calls == 0


def test_commit_failure_after_all_inserts_rolls_back(environment, monkeypatch):
    from sqlalchemy.exc import OperationalError
    e = environment

    def fail(db):
        db.flush()
        # All records have reached SQL, but none has committed yet.
        assert db.scalar(select(func.count()).select_from(GraphSyncJob)) == 1
        assert db.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1
        raise OperationalError("private SQL", {}, RuntimeError("secret"))

    with monkeypatch.context() as patch:
        patch.setattr(e.sessions.class_, "commit", fail)
        result = e.client.post(URL, json=payload(), headers={"Idempotency-Key": "retryable"})
        assert result.status_code == 500 and "secret" not in result.text
        assert counts(e) == (0, 0, 0)
    assert e.client.post(URL, json=payload(), headers={"Idempotency-Key": "retryable"}).status_code == 201


def test_default_wiring_binds_checker_and_graph_to_same_population(environment, monkeypatch):
    e = environment
    settings = Settings(fraud_scoring_enabled=True, graph_population_verified=True,
                        graph_population_name="verified-epoch", graph_sync_adapter="neptune",
                        neptune_endpoint="fake.invalid")
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    monkeypatch.setattr("app.api.scoring_dependencies._graph", lambda *args: e.graph)
    monkeypatch.setattr("app.services.fraud_inference_service.get_fraud_inference_service",
                        lambda *args: FraudInferenceService(e.predictor))
    gate = ingestion_gate(e.engine)
    service = build_scoring_service(e.engine, gate)
    assert service.graph is service.freshness.population.graph is e.graph
    assert service.freshness.population.identifier == neptune_population_id(settings, e.engine)
    assert service.freshness.gate is gate


@pytest.mark.parametrize("backend", ["memory", "postgresql"])
def test_unsupported_source_refused_even_with_verified_settings(environment, monkeypatch, backend):
    from fastapi import HTTPException
    settings = Settings(fraud_scoring_enabled=True, graph_population_verified=True,
                        graph_population_name="epoch", graph_sync_adapter="neptune",
                        neptune_endpoint="fake.invalid")
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    engine = (make_engine("sqlite:///:memory:") if backend == "memory"
              else SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    try:
        with pytest.raises(HTTPException) as error:
            build_scoring_service(engine, IngestionGate())
        assert error.value.status_code == 503
    finally:
        if backend == "memory":
            engine.dispose()
