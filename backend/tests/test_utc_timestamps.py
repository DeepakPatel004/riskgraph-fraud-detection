from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, make_engine
from app.core.types import UTCDateTime
from app.models import FraudAlert, GraphSyncJob, IdempotencyRecord, Transaction


@pytest.fixture
def engine():
    engine = make_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def transaction(transaction_id, event_time):
    return Transaction(
        transaction_id=transaction_id, amount="123.45", card_token="DEMO_CARD",
        event_time=event_time,
    )


def test_equivalent_instants_round_trip_as_utc(engine):
    with Session(engine) as db:
        db.add_all([
            transaction("OFFSET", datetime.fromisoformat("2026-10-10T10:30:00+05:30")),
            transaction("UTC", datetime.fromisoformat("2026-10-10T05:00:00Z")),
        ])
        db.commit()
    with Session(engine) as db:
        offset = db.get(Transaction, "OFFSET").event_time
        utc = db.get(Transaction, "UTC").event_time
        assert offset == utc == datetime(2026, 10, 10, 5, tzinfo=timezone.utc)
        assert offset.tzinfo is timezone.utc
        assert utc.tzinfo is timezone.utc
        stored = db.connection().exec_driver_sql(
            "SELECT event_time FROM transactions ORDER BY transaction_id"
        ).scalars().all()
        assert stored == ["2026-10-10 05:00:00.000000"] * 2


def test_naive_new_database_write_is_rejected(engine):
    with Session(engine) as db:
        db.add(transaction("NAIVE", datetime(2026, 10, 10, 5)))
        with pytest.raises(StatementError, match="Timestamp must include a timezone"):
            db.commit()
        db.rollback()
        assert db.get(Transaction, "NAIVE") is None


def test_legacy_naive_values_are_read_as_utc_without_rewriting(engine):
    # Simulate the storage format produced by the old application.
    with engine.begin() as connection:
        connection.exec_driver_sql("""
            INSERT INTO transactions
            (transaction_id, amount, currency, card_token, event_time, features_json,
             processing_status, graph_sync_status, created_at)
            VALUES ('LEGACY', 123.45, 'INR', 'DEMO', '2026-10-10 05:00:00.123456',
                    '{}', 'RECEIVED', 'PENDING', '2026-10-10 06:00:00.000000')
        """)
    with Session(engine) as db:
        row = db.get(Transaction, "LEGACY")
        assert row.event_time == datetime(2026, 10, 10, 5, 0, 0, 123456, tzinfo=timezone.utc)
        assert row.event_time.tzinfo is timezone.utc
        assert row.created_at.tzinfo is timezone.utc
        assert db.connection().exec_driver_sql(
            "SELECT event_time FROM transactions WHERE transaction_id='LEGACY'"
        ).scalar_one() == "2026-10-10 05:00:00.123456"


def test_all_system_timestamps_round_trip_as_utc(engine):
    old_time = datetime(2000, 1, 1, tzinfo=timezone.utc)
    with Session(engine) as db:
        row = transaction("SYSTEM", datetime.now(timezone.utc))
        db.add(row)
        db.flush()
        alert = FraudAlert(transaction_id="SYSTEM", risk_score=0.9)
        record = IdempotencyRecord(
            idempotency_key="system", request_hash="a" * 64,
            transaction_id="SYSTEM", status="COMPLETED",
        )
        job = GraphSyncJob(transaction_id="SYSTEM")
        db.add_all([alert, record, job])
        db.flush()
        for value in [row.created_at, alert.created_at, alert.updated_at,
                      record.created_at, job.next_attempt_at]:
            assert value.tzinfo is timezone.utc
        alert_id, job_id = alert.alert_id, job.job_id
        db.commit()
    with Session(engine) as db:
        alert = db.get(FraudAlert, alert_id)
        for value in [db.get(Transaction, "SYSTEM").created_at,
                      alert.created_at, alert.updated_at,
                      db.get(IdempotencyRecord, "system").created_at,
                      db.get(GraphSyncJob, job_id).next_attempt_at]:
            assert value.tzinfo is timezone.utc
        alert.updated_at = old_time
        db.commit()
        alert.investigation_status = "CLOSED"
        db.commit()
        db.refresh(alert)
        assert alert.updated_at.tzinfo is timezone.utc
        assert alert.updated_at > old_time


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()],
                         ids=["sqlite", "postgresql"])
def test_dialect_timestamp_conversion(dialect):
    # PostgreSQL conversion is tested without requiring a running server.
    column_type = UTCDateTime()
    value = datetime(2026, 10, 10, 10, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    bound = column_type.process_bind_param(value, dialect)
    expected = datetime(2026, 10, 10, 5, tzinfo=timezone.utc)
    assert bound == (expected.replace(tzinfo=None) if dialect.name == "sqlite" else expected)
    assert column_type.process_result_value(bound, dialect) == expected
    assert column_type.process_result_value(value, dialect).tzinfo is timezone.utc
    assert column_type.process_result_value(value, dialect) == expected
    with pytest.raises(ValueError, match="Timestamp must include a timezone"):
        column_type.process_bind_param(datetime(2026, 10, 10), dialect)
    assert column_type.process_bind_param(None, dialect) is None
    assert column_type.process_result_value(None, dialect) is None
    compiled = str(column_type.compile(dialect=dialect))
    assert compiled == ("DATETIME" if dialect.name == "sqlite" else "TIMESTAMP WITH TIME ZONE")
