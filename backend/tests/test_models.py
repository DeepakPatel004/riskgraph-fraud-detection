from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app.core.database import Base
from app.models import Transaction, FraudAlert


def test_schema_contains_four_tables():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    assert set(inspect(engine).get_table_names()) == {
        "transactions", "fraud_alerts", "idempotency_records", "graph_sync_jobs"
    }
    engine.dispose()


def test_transaction_and_alert_persist():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Transaction(transaction_id="T1", amount=Decimal("123.45"), card_token="DEMO_CARD", event_time=datetime.now(timezone.utc)))
        db.add(FraudAlert(transaction_id="T1", risk_score=0.9, risk_indicators=["test indicator"]))
        db.commit()
        assert db.get(Transaction, "T1").amount == Decimal("123.45")
        assert db.query(FraudAlert).count() == 1
    engine.dispose()
