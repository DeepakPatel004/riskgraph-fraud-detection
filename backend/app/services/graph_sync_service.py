"""Transactional outbox processing with bounded attempts and fenced leases."""

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Callable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.graph.models import TransactionNode, as_utc
from app.graph.repository import GraphRepository
from app.models import GraphSyncJob, Transaction
from app.models.entities import utc_now


logger = logging.getLogger(__name__)
SessionFactory = Callable[[], Session]


class GraphSyncOptions(BaseModel):
    model_config = ConfigDict(frozen=True)
    initial_delay: float = Field(default=5, gt=0, allow_inf_nan=False)
    backoff_factor: float = Field(default=2, ge=1, allow_inf_nan=False)
    max_delay: float = Field(default=300, gt=0, allow_inf_nan=False)
    max_attempts: int = Field(default=5, ge=1)
    lease_seconds: float = Field(default=300, gt=0, allow_inf_nan=False)

    def retry_delay(self, attempt: int) -> float:
        """Attempt 1 waits 5s, then 10s, 20s, ... capped at 300s."""
        delay = min(self.initial_delay, self.max_delay)
        for _ in range(attempt - 1):
            delay = min(delay * self.backoff_factor, self.max_delay)
            if delay == self.max_delay:
                break
        return delay


class GraphSyncPersistenceError(RuntimeError):
    """Sanitized SQL failure; a persisted lease remains recoverable on expiry."""


class GraphSyncLeaseLost(RuntimeError):
    """Ownership expired or changed before the external write started."""


@dataclass(frozen=True)
class ClaimedJob:
    job_id: str
    transaction_id: str
    lease_token: str
    attempts: int
    terminal_recovery: bool = False


def claim_query(now: datetime, dialect_name: str):
    """PostgreSQL locks one candidate; SQLite relies on the guarded UPDATE."""
    query = select(GraphSyncJob).where(
        GraphSyncJob.status.in_(["PENDING", "PROCESSING"]), GraphSyncJob.next_attempt_at <= now,
    ).order_by(GraphSyncJob.next_attempt_at, GraphSyncJob.job_id).limit(1)
    return query.with_for_update(skip_locked=True) if dialect_name == "postgresql" else query


def retry_failed_job(session_factory: SessionFactory, job_id: str,
                     now: datetime | None = None) -> bool:
    """Explicit operator retry: reset only a terminal FAILED job, never steal a lease."""
    now = as_utc(now if now is not None else utc_now())
    with session_factory() as db, db.begin():
        job = db.get(GraphSyncJob, job_id)
        if job is None:
            return False
        result = db.execute(update(GraphSyncJob).where(
            GraphSyncJob.job_id == job_id, GraphSyncJob.status == "FAILED",
        ).values(status="PENDING", attempts=0, lease_token=None,
                 next_attempt_at=now, last_error=None))
        if result.rowcount != 1:
            return False
        db.execute(update(Transaction).where(Transaction.transaction_id == job.transaction_id)
                   .values(graph_sync_status="PENDING"))
    return True


class GraphSyncService:
    def __init__(self, session_factory: SessionFactory, graph: GraphRepository,
                 options: GraphSyncOptions | None = None,
                 clock: Callable[[], datetime] = utc_now):
        self.session_factory = session_factory
        self.graph = graph
        self.options = options or GraphSyncOptions()
        self.clock = clock

    def _now(self) -> datetime:
        return as_utc(self.clock())

    def claim_next(self) -> ClaimedJob | None:
        """Commit ownership before graph I/O; CAS protects races on either backend.

        PostgreSQL also skips rows locked by another claimant. SQLite requires a
        single worker operationally; the conditional update still fences stale
        candidates. next_attempt_at is lease expiry while status is PROCESSING.
        """
        now = self._now()
        eligible = (GraphSyncJob.status.in_(["PENDING", "PROCESSING"])
                    & (GraphSyncJob.next_attempt_at <= now))
        with self.session_factory() as db, db.begin():
            query = claim_query(now, db.get_bind().dialect.name)
            job = db.scalar(query)
            if job is None:
                return None
            token = str(uuid4())
            terminal = job.attempts >= self.options.max_attempts
            guard = (GraphSyncJob.job_id == job.job_id) & eligible
            guard &= GraphSyncJob.attempts == job.attempts
            guard &= GraphSyncJob.status == job.status
            guard &= GraphSyncJob.lease_token == job.lease_token
            attempts = job.attempts if terminal else job.attempts + 1
            values = dict(status="FAILED" if terminal else "PROCESSING", attempts=attempts,
                          lease_token=None if terminal else token,
                          next_attempt_at=now if terminal else now + timedelta(seconds=self.options.lease_seconds))
            if terminal:
                values["last_error"] = "Retry limit reached after lease expiry or recovery"
            result = db.execute(update(GraphSyncJob).where(guard).values(**values)
                                .execution_options(synchronize_session=False))
            if result.rowcount != 1:
                return None
            db.execute(update(Transaction).where(Transaction.transaction_id == job.transaction_id)
                       .values(graph_sync_status=values["status"]))
            claim = ClaimedJob(job.job_id, job.transaction_id, token, attempts, terminal)
        return claim

    def _load_transaction(self, claim: ClaimedJob) -> TransactionNode:
        # This read session is closed before calling any graph adapter.
        with self.session_factory() as db:
            owned = db.scalar(select(GraphSyncJob.job_id).where(
                GraphSyncJob.job_id == claim.job_id, GraphSyncJob.status == "PROCESSING",
                GraphSyncJob.lease_token == claim.lease_token,
                GraphSyncJob.next_attempt_at > self._now(),
            ))
            if owned is None:
                raise GraphSyncLeaseLost("Graph sync lease is no longer active")
            row = db.get(Transaction, claim.transaction_id)
            if row is None:
                raise ValueError("Outbox parent is missing")
            return TransactionNode(
                transaction_id=row.transaction_id, amount=row.amount, currency=row.currency,
                card_token=row.card_token, device_token=row.device_token or None, event_time=row.event_time,
            )

    def complete(self, claim: ClaimedJob, *, success: bool) -> bool:
        """Only the current lease owner may publish SQL completion/failure.

        If commit fails after a successful graph write, the existing lease expires
        and the idempotent write can be repeated. A stale owner cannot change a
        newer owner's state, even if its graph call finishes late.
        """
        now = self._now()
        status = "SYNCED" if success else (
            "FAILED" if claim.attempts >= self.options.max_attempts else "PENDING"
        )
        due = now + timedelta(seconds=self.options.retry_delay(claim.attempts)) if status == "PENDING" else now
        with self.session_factory() as db, db.begin():
            result = db.execute(update(GraphSyncJob).where(
                GraphSyncJob.job_id == claim.job_id, GraphSyncJob.status == "PROCESSING",
                GraphSyncJob.lease_token == claim.lease_token,
            ).values(status=status, lease_token=None, next_attempt_at=due,
                     last_error=None if success else "Graph synchronization failed"))
            if result.rowcount != 1:
                return False
            db.execute(update(Transaction).where(Transaction.transaction_id == claim.transaction_id)
                       .values(graph_sync_status=status))
        return True

    def process_batch(self, batch_size: int = 20, should_stop: Callable[[], bool] | None = None) -> int:
        """Handle up to batch_size claims/recoveries, claiming just before each call.

        Attempts count claims, including crashes before the graph call. Never
        pre-claim a batch whose leases could expire while waiting in a local queue.
        """
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        handled = 0
        for _ in range(batch_size):
            if should_stop is not None and should_stop():
                break
            try:
                claim = self.claim_next()
            except SQLAlchemyError:
                raise GraphSyncPersistenceError("Could not persist graph sync claim") from None
            if claim is None:
                break
            handled += 1
            if claim.terminal_recovery:
                continue
            try:
                node = self._load_transaction(claim)
                self.graph.upsert_transaction(node)
                success = True
            except GraphSyncLeaseLost:
                logger.warning("Graph sync lease expired or changed before the graph call")
                continue
            except Exception:
                # Never persist/log str(exc): it may contain tokens or credentials.
                logger.warning("Graph synchronization attempt failed; retry policy will apply")
                success = False
            try:
                owned = self.complete(claim, success=success)
            except SQLAlchemyError:
                raise GraphSyncPersistenceError("Could not persist graph sync completion; lease recovery required") from None
            if not owned:
                logger.warning("Graph synchronization result ignored because lease ownership changed")
        return handled
