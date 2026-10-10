from threading import Event

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.graph.in_memory import InMemoryGraphRepository
from app.models import GraphSyncJob
from app.services.graph_sync_service import GraphSyncPersistenceError
from app.workers.graph_sync_worker import main, run_worker
from tests.test_graph_sync_service import FailingGraph, enqueue, job_state, make_service, store


def test_once_processes_only_one_batch(store):
    _, factory = store
    enqueue(factory, "ONE", "one")
    enqueue(factory, "TWO", "two")
    service, _ = make_service(factory)
    assert run_worker(service, once=True, batch_size=1) == 0
    with factory() as db:
        assert sorted(db.scalars(select(GraphSyncJob.status)).all()) == ["PENDING", "SYNCED"]


def test_shutdown_finishes_current_job_then_stops(store):
    _, factory = store
    enqueue(factory, "ONE", "one")
    enqueue(factory, "TWO", "two")
    stop = Event()
    graph = InMemoryGraphRepository()
    class StopAfterWrite:
        def upsert_transaction(self, node):
            graph.upsert_transaction(node)
            stop.set()
    service, _ = make_service(factory, StopAfterWrite())
    assert run_worker(service, batch_size=20, stop_event=stop) == 0
    with factory() as db:
        assert sorted(db.scalars(select(GraphSyncJob.status)).all()) == ["PENDING", "SYNCED"]


def test_continuous_mode_waits_using_configured_poll_interval(store):
    _, factory = store
    service, _ = make_service(factory)
    class StopAfterPoll:
        stopped = False
        waits = []
        def is_set(self):
            return self.stopped
        def wait(self, seconds):
            self.waits.append(seconds)
            self.stopped = True
    stop = StopAfterPoll()
    assert run_worker(service, poll_interval=0.25, stop_event=stop) == 0
    assert stop.waits == [0.25]


def test_persistence_failure_once_exits_nonzero_with_safe_log(caplog):
    class BrokenService:
        def process_batch(self, *args, **kwargs):
            raise GraphSyncPersistenceError("Could not persist graph sync completion; lease recovery required")
    assert run_worker(BrokenService(), once=True) == 1
    assert "lease recovery required" in caplog.text


@pytest.mark.parametrize("args", [[], ["--adapter", "memory", "--once"]])
def test_worker_requires_explicit_storage_and_ephemeral_acknowledgement(monkeypatch, args):
    monkeypatch.setattr("app.core.config.get_settings", lambda: Settings(_env_file=None))
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2


def test_memory_cli_warns_about_restart_data_loss(store, monkeypatch, caplog):
    _, factory = store
    enqueue(factory)
    monkeypatch.setattr("app.core.database.SessionLocal", factory)
    monkeypatch.setattr("app.core.config.get_settings", lambda: Settings(_env_file=None))
    assert main(["--once", "--adapter", "memory", "--allow-ephemeral"]) == 0
    assert job_state(factory).status == "SYNCED"
    assert "EPHEMERAL GRAPH" in caplog.text


def test_retry_failed_cli_does_not_need_graph_adapter(store, monkeypatch):
    _, factory = store
    enqueue(factory)
    service, _ = make_service(factory, FailingGraph())
    service.options = service.options.model_copy(update={"max_attempts": 1})
    service.process_batch()
    failed_id = job_state(factory).job_id
    monkeypatch.setattr("app.core.database.SessionLocal", factory)
    monkeypatch.setattr("app.core.config.get_settings", lambda: Settings(_env_file=None))
    assert main(["--retry-failed", failed_id]) == 0
    assert job_state(factory).status == "PENDING"


def test_neptune_cli_requires_configuration_without_network_calls(monkeypatch):
    monkeypatch.setattr("app.core.config.get_settings", lambda: Settings(_env_file=None))
    with pytest.raises(SystemExit) as error:
        main(["--adapter", "neptune", "--once"])
    assert error.value.code == 2
