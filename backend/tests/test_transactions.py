import pytest
from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app.core.database import Base, get_db, make_engine
from app.main import app
from app.models import Transaction


@pytest.fixture
def client():
    engine = make_engine(f"sqlite:///file:{uuid4().hex}?mode=memory&cache=shared&uri=true", poolclass=StaticPool)
    Base.metadata.create_all(engine)

    def override_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def payload(txn="TXN-100"):
    return {"transaction_id": txn, "amount": "15000.50", "currency": "INR",
            "card_token": "TOKEN_CARD_22", "device_token": "TOKEN_DEVICE_11",
            "event_time": "2026-10-10T10:30:00Z"}


def test_create_read_and_list(client):
    result = client.post("/api/v1/transactions", json=payload())
    assert result.status_code == 201, result.text
    assert result.json()["processing_status"] == "RECEIVED"
    assert result.json()["risk_score"] is None  # ML arrives in a later step
    assert client.get("/api/v1/transactions/TXN-100").json()["transaction_id"] == "TXN-100"
    listing = client.get("/api/v1/transactions?limit=10&offset=0").json()
    assert listing["total"] == 1
    assert len(listing["items"]) == 1


def test_idempotency_replay_and_conflict(client):
    headers = {"Idempotency-Key": "retry-123"}
    assert client.post("/api/v1/transactions", json=payload(), headers=headers).status_code == 201
    assert client.post("/api/v1/transactions", json=payload(), headers=headers).status_code == 200
    assert client.post("/api/v1/transactions", json=payload("TXN-OTHER"), headers=headers).status_code == 409
    assert client.get("/api/v1/transactions").json()["total"] == 1


def test_duplicate_transaction_id(client):
    assert client.post("/api/v1/transactions", json=payload()).status_code == 201
    assert client.post("/api/v1/transactions", json=payload()).status_code == 409


def test_invalid_data(client):
    invalid = payload()
    invalid["amount"] = "-10"
    assert client.post("/api/v1/transactions", json=invalid).status_code == 422
    invalid = payload()
    invalid["event_time"] = "2026-10-10T10:30:00"
    assert client.post("/api/v1/transactions", json=invalid).status_code == 422
    assert client.get("/api/v1/transactions/missing").status_code == 404
    assert client.get("/api/v1/transactions?limit=101").status_code == 422


def test_pagination(client):
    for n in range(3):
        assert client.post("/api/v1/transactions", json=payload(f"T{n}")).status_code == 201
    data = client.get("/api/v1/transactions?limit=2&offset=1").json()
    assert data["total"] == 3
    assert len(data["items"]) == 2


def test_offset_timestamp_is_returned_as_utc(client):
    data = payload()
    data["event_time"] = "2026-10-10T10:30:00+05:30"
    response = client.post("/api/v1/transactions", json=data)
    assert response.status_code == 201
    assert response.json()["event_time"] == "2026-10-10T05:00:00Z"
    retrieved = client.get("/api/v1/transactions/TXN-100")
    assert retrieved.status_code == 200
    assert retrieved.json()["event_time"] == "2026-10-10T05:00:00Z"


def test_equivalent_offset_idempotent_retry(client):
    data = payload()
    data["event_time"] = "2026-10-10T10:30:00+05:30"
    headers = {"Idempotency-Key": "utc-retry"}
    first = client.post("/api/v1/transactions", json=data, headers=headers)
    assert first.status_code == 201
    data["event_time"] = "2026-10-10T05:00:00Z"
    retry = client.post("/api/v1/transactions", json=data, headers=headers)
    assert retry.status_code == 200
    assert retry.json() == first.json()
    assert client.get("/api/v1/transactions").json()["total"] == 1


def test_ordering_uses_instants_across_offsets(client):
    for transaction_id, event_time in [
        ("MIDDLE", "2026-10-10T10:30:00+05:30"),  # 05:00 UTC
        ("NEWEST", "2026-10-10T05:15:00Z"),
        ("OLDEST", "2026-10-10T00:45:00-04:00"),  # 04:45 UTC
    ]:
        data = payload(transaction_id)
        data["event_time"] = event_time
        assert client.post("/api/v1/transactions", json=data).status_code == 201
    items = client.get("/api/v1/transactions").json()["items"]
    assert [item["transaction_id"] for item in items] == ["NEWEST", "MIDDLE", "OLDEST"]
    assert all(item["event_time"].endswith("Z") for item in items)


@pytest.mark.parametrize("event_time", ["2026-10-10T05:00:00", "2026-10-10"])
def test_naive_timestamp_is_rejected(client, event_time):
    data = payload()
    data["event_time"] = event_time
    response = client.post("/api/v1/transactions", json=data)
    assert response.status_code == 422
    assert client.get("/api/v1/transactions").json()["total"] == 0
