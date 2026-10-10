import pytest
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from app.core.database import Base, make_engine
from app.models import IdempotencyRecord, Transaction
from app.schemas.transaction import TransactionCreate
from app.services.transaction_service import create_transaction


@pytest.fixture
def engine():
    # A named in-memory database can be shared by separate test connections.
    engine = make_engine(f"sqlite:///file:{uuid4().hex}?mode=memory&cache=shared&uri=true", poolclass=QueuePool)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def payload(transaction_id="INTEGRITY-1"):
    return TransactionCreate(
        transaction_id=transaction_id, amount="123.45", card_token="DEMO_CARD",
        event_time="2026-10-10T10:30:00Z",
    )


def test_foreign_keys_enabled_on_each_connection(engine):
    # Holding both connections open forces the pool to open distinct connections.
    with engine.connect() as first, engine.connect() as second:
        assert first.connection.driver_connection is not second.connection.driver_connection
        assert first.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert second.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    engine.dispose()
    with engine.connect() as replacement:
        assert replacement.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_foreign_key_rejects_orphan_record(engine):
    with Session(engine) as db:
        db.add(IdempotencyRecord(
            idempotency_key="orphan", request_hash="a" * 64,
            transaction_id="MISSING", status="COMPLETED",
        ))
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            db.commit()
        db.rollback()
        assert db.get(IdempotencyRecord, "orphan") is None


def test_idempotent_create_and_sequential_retry(engine):
    with Session(engine) as db:
        transaction, created = create_transaction(db, payload(), "retry")
        assert created is True
        assert transaction.transaction_id == "INTEGRITY-1"
    # Read through a fresh session to verify both records were committed.
    with Session(engine) as db:
        record = db.get(IdempotencyRecord, "retry")
        assert record.transaction_id == "INTEGRITY-1"
        transaction, created = create_transaction(db, payload(), "retry")
        assert created is False
        assert transaction.transaction_id == record.transaction_id
        assert len(db.scalars(select(Transaction)).all()) == 1


def test_idempotency_key_with_different_data_conflicts(engine):
    with Session(engine) as db:
        create_transaction(db, payload(), "retry")
        with pytest.raises(HTTPException) as error:
            create_transaction(db, payload("OTHER"), "retry")
        assert error.value.status_code == 409
        assert db.get(Transaction, "OTHER") is None


def test_failed_idempotency_insert_rolls_back_parent(engine):
    # A real database failure after the parent flush must undo both inserts.
    with engine.begin() as connection:
        connection.exec_driver_sql("""
            CREATE TRIGGER reject_idempotency_insert
            BEFORE INSERT ON idempotency_records
            BEGIN
                SELECT RAISE(ABORT, 'forced idempotency insertion failure');
            END
        """)
    with Session(engine) as db:
        with pytest.raises(IntegrityError, match="forced idempotency insertion failure"):
            create_transaction(db, payload(), "retry")
    with Session(engine) as db:
        assert db.get(Transaction, "INTEGRITY-1") is None
        assert db.get(IdempotencyRecord, "retry") is None
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER reject_idempotency_insert")
    with Session(engine) as db:
        _, created = create_transaction(db, payload(), "retry")
        assert created is True


@pytest.mark.parametrize("scenario", ["duplicate_id", "matching_key", "conflicting_key"])
def test_conflict_after_initial_checks(engine, scenario):
    key = None if scenario == "duplicate_id" else "race-key"
    with Session(engine) as db:
        # Commit a competing request between the initial lookups and insertion.
        @event.listens_for(db, "before_flush", once=True)
        def competing_request(session, flush_context, instances):
            competing_payload = payload("OTHER") if scenario == "conflicting_key" else payload()
            with Session(engine) as competitor:
                create_transaction(competitor, competing_payload, key)

        if scenario == "matching_key":
            transaction, created = create_transaction(db, payload(), key)
            assert created is False
            assert transaction.transaction_id == "INTEGRITY-1"
        else:
            with pytest.raises(HTTPException) as error:
                create_transaction(db, payload(), key)
            assert error.value.status_code == 409
    with Session(engine) as db:
        assert len(db.scalars(select(Transaction)).all()) == 1
