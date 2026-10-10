from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, inspect, select
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool
from sqlalchemy.dialects import postgresql, sqlite

from app.core.database import Base, get_db, make_engine
from app.graph.in_memory import InMemoryGraphRepository
from app.graph.models import TransactionNode
from app.main import app
from app.models import GraphSyncJob, IdempotencyRecord, Transaction
from app.schemas.transaction import TransactionCreate
from app.services.graph_sync_service import (
    GraphSyncLeaseLost, GraphSyncOptions, GraphSyncPersistenceError, GraphSyncService, claim_query, retry_failed_job,
)
from app.services.transaction_service import create_transaction
from scripts.migrate_graph_sync_leases import upgrade_graph_sync_leases


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def store():
    engine = make_engine(f"sqlite:///file:{uuid4().hex}?mode=memory&cache=shared&uri=true", poolclass=QueuePool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield engine, factory
    finally:
        engine.dispose()


def enqueue(factory, identifier="OUTBOX-1", key="retry"):
    with factory() as db:
        return create_transaction(db, TransactionCreate(
            transaction_id=identifier, amount="123.45", card_token="TOKEN_CARD",
            device_token="TOKEN_DEVICE", event_time="2026-10-10T10:30:00+05:30",
        ), key)[0]


def job_state(factory):
    with factory() as db:
        job = db.scalar(select(GraphSyncJob))
        transaction = db.get(Transaction, job.transaction_id)
        assert transaction.graph_sync_status == job.status
        return job


def make_service(factory, graph=None, options=None):
    clock = Clock()
    service = GraphSyncService(factory, graph if graph is not None else InMemoryGraphRepository(), options, clock)
    # Ensure synthetic test time is after the real enqueue time, without sleeps.
    with factory.begin() as db:
        for job in db.scalars(select(GraphSyncJob)):
            job.next_attempt_at = clock.now
    return service, clock


def test_outbox_create_replay_duplicate_and_single_commit(store):
    _, factory = store
    commits = []
    with factory() as db:
        event.listen(db, "after_commit", lambda session: commits.append(1))
        payload = TransactionCreate(transaction_id="T1", amount="1", card_token="CARD", event_time="2026-10-10T00:00:00Z")
        assert create_transaction(db, payload, "same")[1] is True
        assert create_transaction(db, payload, "same")[1] is False
        with pytest.raises(HTTPException) as error:
            create_transaction(db, payload, None)
        assert error.value.status_code == 409
        assert len(commits) == 1
    with factory() as db:
        assert len(db.scalars(select(GraphSyncJob)).all()) == 1
        assert len(db.scalars(select(IdempotencyRecord)).all()) == 1
        assert db.scalar(select(GraphSyncJob)).status == "PENDING"


@pytest.mark.parametrize("table", ["graph_sync_jobs", "idempotency_records"])
def test_any_outbox_insert_failure_rolls_back_everything(store, table):
    engine, factory = store
    with engine.begin() as connection:
        connection.exec_driver_sql(f"""
            CREATE TRIGGER reject_insert BEFORE INSERT ON {table}
            BEGIN SELECT RAISE(ABORT, 'forced insertion failure'); END
        """)
    with pytest.raises(IntegrityError, match="forced insertion failure"):
        enqueue(factory)
    with factory() as db:
        for model in [Transaction, GraphSyncJob, IdempotencyRecord]:
            assert db.scalars(select(model)).all() == []


def test_successful_sync_closes_sql_sessions_before_graph_call(store):
    engine, factory = store
    enqueue(factory)
    sessions = []
    class TrackedSession(Session):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            sessions.append(self)
    tracked = sessionmaker(engine, class_=TrackedSession)
    graph = InMemoryGraphRepository()
    class InspectingGraph:
        def upsert_transaction(self, node):
            assert all(not session.in_transaction() for session in sessions)
            assert job_state(factory).status == "PROCESSING"  # Claim is committed.
            assert isinstance(node, TransactionNode)
            assert node.event_time.tzinfo is timezone.utc
            graph.upsert_transaction(node)
    service, _ = make_service(tracked, InspectingGraph())
    assert service.process_batch() == 1
    job = job_state(factory)
    assert job.status == "SYNCED" and job.attempts == 1
    assert job.lease_token is None and job.last_error is None
    network = graph.get_transaction_network("OUTBOX-1")
    assert len(network.nodes) == 3 and len(network.relationships) == 2
    assert service.process_batch() == 0


class FailingGraph:
    def upsert_transaction(self, node):
        raise RuntimeError("TOKEN_CARD TOKEN_DEVICE fake-secret-credential")


def test_retry_backoff_cap_max_attempts_and_sanitized_errors(store, caplog):
    _, factory = store
    enqueue(factory)
    service, clock = make_service(factory, FailingGraph(), GraphSyncOptions(max_delay=12))
    delays = [5, 10, 12, 12]
    for attempt in range(1, 6):
        before = clock.now
        assert service.process_batch() == 1
        job = job_state(factory)
        assert job.attempts == attempt
        assert job.last_error == "Graph synchronization failed"
        assert job.next_attempt_at.tzinfo is timezone.utc
        assert job.lease_token is None
        if attempt < 5:
            assert job.status == "PENDING"
            assert job.next_attempt_at == before + timedelta(seconds=delays[attempt - 1])
            assert service.process_batch() == 0
            clock.now = job.next_attempt_at
        else:
            assert job.status == "FAILED"
    clock.now += timedelta(days=1)
    assert service.process_batch() == 0
    for sensitive in ["TOKEN_CARD", "TOKEN_DEVICE", "fake-secret-credential"]:
        assert sensitive not in caplog.text
        assert sensitive not in job.last_error
    with factory() as db:
        assert db.get(Transaction, "OUTBOX-1") is not None


def test_temporary_failure_then_success(store):
    _, factory = store
    enqueue(factory)
    service, clock = make_service(factory, FailingGraph())
    service.process_batch()
    clock.now += timedelta(seconds=5)
    service.graph = InMemoryGraphRepository()
    assert service.process_batch() == 1
    job = job_state(factory)
    assert job.status == "SYNCED" and job.attempts == 2 and job.last_error is None


def test_expired_lease_is_reclaimed_and_stale_completion_is_fenced(store):
    _, factory = store
    enqueue(factory)
    service, clock = make_service(factory, options=GraphSyncOptions(lease_seconds=10))
    first = service.claim_next()  # Simulate a process crashing here.
    assert service.claim_next() is None
    clock.now += timedelta(seconds=10)
    second = service.claim_next()
    assert second.attempts == 2 and second.lease_token != first.lease_token
    assert service.complete(first, success=True) is False
    assert service.complete(first, success=False) is False
    assert job_state(factory).lease_token == second.lease_token
    service.graph.upsert_transaction(service._load_transaction(second))
    assert service.complete(second, success=True) is True
    assert job_state(factory).status == "SYNCED"


def test_last_attempt_crash_becomes_terminal_after_lease_expiry(store):
    _, factory = store
    enqueue(factory)
    service, clock = make_service(factory, options=GraphSyncOptions(max_attempts=1, lease_seconds=10))
    assert service.claim_next().attempts == 1
    clock.now += timedelta(seconds=10)
    assert service.process_batch() == 1
    job = job_state(factory)
    assert job.status == "FAILED" and job.attempts == 1 and job.lease_token is None
    assert service.graph.get_transaction_network("OUTBOX-1").nodes == ()


@pytest.mark.parametrize("table, column", [("graph_sync_jobs", "status"), ("transactions", "graph_sync_status")])
def test_completion_persistence_failure_keeps_recoverable_lease(store, table, column):
    engine, factory = store
    enqueue(factory)
    service, clock = make_service(factory, options=GraphSyncOptions(lease_seconds=10))
    with engine.begin() as connection:
        connection.exec_driver_sql(f"""
            CREATE TRIGGER reject_completion BEFORE UPDATE ON {table}
            WHEN NEW.{column} = 'SYNCED'
            BEGIN SELECT RAISE(ABORT, 'completion persistence failure'); END
        """)
    with pytest.raises(GraphSyncPersistenceError, match="lease recovery required"):
        service.process_batch()
    job = job_state(factory)
    assert job.status == "PROCESSING" and job.attempts == 1 and job.lease_token
    assert len(service.graph.get_transaction_network("OUTBOX-1").relationships) == 2
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER reject_completion")
    clock.now += timedelta(seconds=10)
    assert service.process_batch() == 1
    assert job_state(factory).status == "SYNCED"
    assert job_state(factory).attempts == 2
    assert len(service.graph.get_transaction_network("OUTBOX-1").relationships) == 2


def test_completion_commit_failure_rolls_back_and_remains_retryable(store):
    engine, factory = store
    enqueue(factory)
    class CommitFailureSession(Session):
        pass
    armed = [True]
    @event.listens_for(CommitFailureSession, "before_commit")
    def fail_completion_commit(db):
        job = db.scalar(select(GraphSyncJob))
        if armed[0] and job.status == "SYNCED":
            armed[0] = False
            raise SQLAlchemyError("sensitive SQL details")
    service, clock = make_service(sessionmaker(engine, class_=CommitFailureSession), options=GraphSyncOptions(lease_seconds=10))
    with pytest.raises(GraphSyncPersistenceError) as error:
        service.process_batch()
    assert "sensitive" not in str(error.value)
    assert job_state(factory).status == "PROCESSING"
    clock.now += timedelta(seconds=10)
    service.process_batch()
    assert job_state(factory).status == "SYNCED"


def test_two_connections_cannot_both_claim_same_active_job(store):
    engine, factory = store
    enqueue(factory)
    service, _ = make_service(factory)
    barrier = Barrier(2)
    @event.listens_for(engine, "before_cursor_execute")
    def interleave_claims(connection, cursor, statement, parameters, context, many):
        if statement.startswith("UPDATE graph_sync_jobs SET"):
            barrier.wait(timeout=10)
    def claim():
        try:
            return service.claim_next()
        except OperationalError as error:
            # Shared-cache SQLite can reject a concurrent write with SQLITE_LOCKED.
            # Operationally only one SQLite worker is supported; a failed claim
            # must not proceed to graph I/O.
            assert "locked" in str(error).lower()
            return None
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: claim(), range(2)))
    finally:
        event.remove(engine, "before_cursor_execute", interleave_claims)
    assert len([result for result in results if result is not None]) == 1
    job = job_state(factory)
    assert job.attempts == 1 and job.status == "PROCESSING"


def test_manual_retry_resets_only_terminal_failures(store):
    _, factory = store
    enqueue(factory)
    service, clock = make_service(factory, FailingGraph(), GraphSyncOptions(max_attempts=1))
    service.process_batch()
    job = job_state(factory)
    assert retry_failed_job(factory, job.job_id, clock.now)
    job = job_state(factory)
    assert job.status == "PENDING" and job.attempts == 0 and job.last_error is None
    assert not retry_failed_job(factory, job.job_id, clock.now)
    assert not retry_failed_job(factory, "missing", clock.now)
    claim = service.claim_next()
    assert not retry_failed_job(factory, claim.job_id, clock.now)
    assert job_state(factory).lease_token == claim.lease_token


def test_memory_restart_loses_graph_but_does_not_automatically_replay_synced_jobs(store):
    _, factory = store
    enqueue(factory)
    service, _ = make_service(factory)
    service.process_batch()
    service.graph = InMemoryGraphRepository()
    assert service.process_batch() == 0
    assert service.graph.get_transaction_network("OUTBOX-1").nodes == ()
    assert job_state(factory).status == "SYNCED"


def test_api_response_contract_preserved_with_outbox(store):
    _, factory = store
    def override():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = override
    payload = dict(transaction_id="API", amount="1.00", card_token="TOKEN", event_time="2026-10-10T05:00:00Z")
    try:
        with TestClient(app) as client:
            created = client.post("/api/v1/transactions", json=payload, headers={"Idempotency-Key": "api"})
            assert created.status_code == 201
            assert created.json() == dict(payload, currency="INR", device_token=None,
                                          processing_status="RECEIVED", graph_sync_status="PENDING",
                                          risk_score=None, risk_level=None)
            assert client.post("/api/v1/transactions", json=payload, headers={"Idempotency-Key": "api"}).status_code == 200
            assert client.post("/api/v1/transactions", json=payload).status_code == 409
            assert client.get("/api/v1/transactions/API").json() == created.json()
            assert client.get("/api/v1/transactions").json()["total"] == 1
        assert job_state(factory).status == "PENDING"
    finally:
        app.dependency_overrides.clear()


def test_migration_preserves_legacy_rows_and_is_repeatable():
    engine = make_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("""CREATE TABLE graph_sync_jobs (
                job_id VARCHAR(36) PRIMARY KEY, transaction_id VARCHAR(64) UNIQUE NOT NULL,
                attempts INTEGER NOT NULL, status VARCHAR(16) NOT NULL,
                next_attempt_at DATETIME NOT NULL, last_error TEXT)
            """)
            connection.exec_driver_sql("""INSERT INTO graph_sync_jobs VALUES
                ('old', 'old-txn', 2, 'PENDING', '2026-10-10 00:00:00.000000', 'safe old error')
            """)
        assert upgrade_graph_sync_leases(engine) is True
        assert upgrade_graph_sync_leases(engine) is False
        with engine.connect() as connection:
            row = connection.exec_driver_sql("SELECT * FROM graph_sync_jobs").one()
            assert tuple(row) == ('old', 'old-txn', 2, 'PENDING', '2026-10-10 00:00:00.000000', 'safe old error', None)
            assert inspect(connection).get_columns("graph_sync_jobs")[-1]["nullable"]
    finally:
        engine.dispose()


def test_default_retry_policy_caps_at_five_minutes():
    options = GraphSyncOptions()
    assert options.max_attempts == 5
    assert [options.retry_delay(attempt) for attempt in range(1, 9)] == [5, 10, 20, 40, 80, 160, 300, 300]


def test_stale_claim_cannot_load_payload_for_graph_write(store):
    _, factory = store
    enqueue(factory)
    service, clock = make_service(factory, options=GraphSyncOptions(lease_seconds=10))
    claim = service.claim_next()
    clock.now += timedelta(seconds=10)
    with pytest.raises(GraphSyncLeaseLost):
        service._load_transaction(claim)
    replacement = service.claim_next()
    with pytest.raises(GraphSyncLeaseLost):
        service._load_transaction(claim)
    assert service._load_transaction(replacement).transaction_id == "OUTBOX-1"


def test_service_rejects_naive_clock(store):
    _, factory = store
    service = GraphSyncService(factory, InMemoryGraphRepository(), clock=lambda: datetime(2026, 10, 10))
    with pytest.raises(ValueError, match="timezone"):
        service.claim_next()


def test_postgresql_claim_locking_sql_is_generated_without_a_live_server():
    now = Clock()()
    pg_sql = str(claim_query(now, "postgresql").compile(dialect=postgresql.dialect()))
    sqlite_sql = str(claim_query(now, "sqlite").compile(dialect=sqlite.dialect()))
    assert "FOR UPDATE SKIP LOCKED" in pg_sql
    assert "FOR UPDATE" not in sqlite_sql


def test_failed_claim_commit_does_not_consume_attempt_or_write_graph(store):
    engine, factory = store
    enqueue(factory)
    service, _ = make_service(factory)
    with engine.begin() as connection:
        connection.exec_driver_sql("""CREATE TRIGGER reject_claim BEFORE UPDATE ON graph_sync_jobs
            WHEN NEW.status = 'PROCESSING'
            BEGIN SELECT RAISE(ABORT, 'sensitive database details'); END
        """)
    with pytest.raises(GraphSyncPersistenceError) as error:
        service.process_batch()
    assert "sensitive" not in str(error.value)
    job = job_state(factory)
    assert job.status == "PENDING" and job.attempts == 0 and job.lease_token is None
    assert service.graph.get_transaction_network("OUTBOX-1").nodes == ()
