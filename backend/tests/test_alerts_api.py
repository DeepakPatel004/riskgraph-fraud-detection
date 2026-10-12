"""Alert workflow uses real temporary SQL and the existing fake scoring setup."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.models import FraudAlert, GraphSyncJob, IdempotencyRecord, Transaction
from tests.test_scoring_api import URL, counts, environment, payload, seed


ALERTS = "/api/v1/alerts"


def create_alert(e, name="scored-1", key=None):
    headers = {"Idempotency-Key": key} if key else {}
    response = e.client.post(URL, json=payload(name), headers=headers)
    assert response.status_code == 201, response.text
    with e.sessions() as db:
        alert = db.scalar(select(FraudAlert).where(FraudAlert.transaction_id == name))
        assert alert is not None
        return alert.alert_id, response.json()


def alert_count(e):
    with e.sessions() as db:
        return db.scalar(select(func.count()).select_from(FraudAlert))


def test_high_alert_detail_matches_saved_score_and_no_tokens(environment):
    e = environment
    seed(e)
    alert_id, score = create_alert(e)
    response = e.client.get(f"{ALERTS}/{alert_id}")
    assert response.status_code == 200
    result = response.json()
    assert set(result) == {"alert_id", "transaction_id", "risk_score", "risk_indicators",
                           "investigation_status", "created_at", "updated_at"}
    assert result["alert_id"] == alert_id and result["transaction_id"] == "scored-1"
    assert result["risk_score"] == score["risk_score"]
    assert result["risk_indicators"] == score["risk_indicators"] == ["AMOUNT_AT_LEAST_3X_CARD_AVERAGE_7D"]
    assert result["investigation_status"] == "OPEN"
    assert result["created_at"].endswith("Z") and result["updated_at"].endswith("Z")
    assert "card-pseudonym" not in response.text and "device-pseudonym" not in response.text


def test_keyed_replay_does_not_create_or_reopen_alert(environment):
    e = environment
    seed(e)
    alert_id, first = create_alert(e, key="one-alert")
    closed = e.client.patch(f"{ALERTS}/{alert_id}", json={"investigation_status": "CLOSED"})
    assert closed.status_code == 200
    e.factory_error = RuntimeError("must not run inference again")
    replay = e.client.post(URL, json=payload(), headers={"Idempotency-Key": "one-alert"})
    assert replay.status_code == 200 and replay.json() == first
    assert e.graph.calls == e.predictor.calls == 1 and alert_count(e) == 1
    detail = e.client.get(f"{ALERTS}/{alert_id}").json()
    assert detail["investigation_status"] == "CLOSED"
    assert detail["risk_indicators"] == first["risk_indicators"]
    assert detail["updated_at"] == closed.json()["updated_at"]


def test_low_score_and_legacy_ingestion_create_no_alert(environment):
    e = environment
    e.predictor.score = 0.2
    assert e.client.post(URL, json=payload(), headers={"Idempotency-Key": "low"}).status_code == 201
    assert e.client.post(URL, json=payload(), headers={"Idempotency-Key": "low"}).status_code == 200
    legacy = payload("legacy")
    legacy.pop("ProductCD")
    assert e.client.post("/api/v1/transactions", json=legacy).status_code == 201
    assert alert_count(e) == 0
    assert e.client.get(ALERTS).json() == {"items": [], "total": 0, "limit": 20, "offset": 0}


def test_replay_of_pre_phase_high_score_does_not_backfill_alert(environment):
    e = environment
    alert_id, first = create_alert(e, key="older-high")
    # Simulate a previously scored HIGH row from before automatic alert creation.
    with e.sessions() as db:
        db.delete(db.get(FraudAlert, alert_id))
        db.commit()
    e.factory_error = RuntimeError("must not call graph or model")
    replay = e.client.post(URL, json=payload(), headers={"Idempotency-Key": "older-high"})
    assert replay.status_code == 200 and replay.json() == first
    assert alert_count(e) == 0 and e.graph.calls == e.predictor.calls == 1


def test_unique_transaction_alert_relationship_is_preserved(environment):
    e = environment
    alert_id, score = create_alert(e)
    with e.sessions() as db:
        db.add(FraudAlert(transaction_id="scored-1", risk_score=score["risk_score"]))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    assert alert_count(e) == 1
    assert e.client.get(f"{ALERTS}/{alert_id}").status_code == 200


def test_list_pagination_and_filter_have_stable_order_and_filtered_total(environment):
    e = environment
    ids = [create_alert(e, f"scored-{i}")[0] for i in range(3)]
    # Give fixtures explicit times to verify sorting independently of clock speed.
    base = datetime(2026, 10, 10, tzinfo=timezone.utc)
    with e.sessions() as db:
        for i, alert_id in enumerate(ids):
            row = db.get(FraudAlert, alert_id)
            row.created_at = base + timedelta(seconds=i)
            row.investigation_status = "IN_REVIEW" if i == 1 else "OPEN"
        db.commit()
    page = e.client.get(ALERTS, params={"limit": 1, "offset": 1}).json()
    assert (page["total"], page["limit"], page["offset"]) == (3, 1, 1)
    assert [row["alert_id"] for row in page["items"]] == [ids[1]]
    filtered = e.client.get(ALERTS, params={"investigation_status": "OPEN", "limit": 1}).json()
    assert filtered["total"] == 2 and filtered["items"][0]["alert_id"] == ids[2]
    review = e.client.get(ALERTS, params={"investigation_status": "IN_REVIEW"}).json()
    assert review["total"] == 1 and review["items"][0]["alert_id"] == ids[1]
    assert e.client.get(ALERTS, params={"investigation_status": "CLOSED"}).json()["total"] == 0
    assert e.client.get(ALERTS, params={"offset": 100}).json()["items"] == []
    assert "card-pseudonym" not in e.client.get(ALERTS).text
    assert "device-pseudonym" not in e.client.get(ALERTS).text


def test_equal_created_times_have_id_tie_breaker(environment):
    e = environment
    ids = [create_alert(e, f"same-time-{i}")[0] for i in range(2)]
    with e.sessions() as db:
        for alert_id in ids:
            db.get(FraudAlert, alert_id).created_at = datetime(2026, 10, 10, tzinfo=timezone.utc)
        db.commit()
    rows = e.client.get(ALERTS).json()["items"]
    assert [row["alert_id"] for row in rows] == sorted(ids, reverse=True)


def test_status_flow_changes_only_status_and_updated_at(environment):
    e = environment
    alert_id, score = create_alert(e, key="flow")
    with e.sessions() as db:
        db.get(FraudAlert, alert_id).updated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        db.commit()
    original = e.client.get(f"{ALERTS}/{alert_id}").json()
    previous = datetime.fromisoformat(original["updated_at"])
    before_counts, before_calls = counts(e), (e.graph.calls, e.predictor.calls)
    for status in ("IN_REVIEW", "CLOSED", "OPEN"):
        started = datetime.now(timezone.utc)
        response = e.client.patch(f"{ALERTS}/{alert_id}", json={"investigation_status": status})
        finished = datetime.now(timezone.utc)
        assert response.status_code == 200
        data = response.json()
        assert data["investigation_status"] == status
        updated = datetime.fromisoformat(data["updated_at"])
        assert started <= updated <= finished and updated > previous
        assert {key: value for key, value in data.items() if key not in {"updated_at", "investigation_status"}} == {
            key: value for key, value in original.items() if key not in {"updated_at", "investigation_status"}}
        previous = updated
    assert counts(e) == before_counts and (e.graph.calls, e.predictor.calls) == before_calls
    with e.sessions() as db:
        transaction = db.get(Transaction, "scored-1")
        assert transaction.risk_score == score["risk_score"] and transaction.risk_level == "HIGH"
        assert db.get(IdempotencyRecord, "flow").status == "COMPLETED"
        assert db.scalar(select(GraphSyncJob)).status == "PENDING"


def test_patching_same_status_preserves_updated_at(environment):
    e = environment
    alert_id, _ = create_alert(e)
    before = e.client.get(f"{ALERTS}/{alert_id}").json()
    result = e.client.patch(f"{ALERTS}/{alert_id}", json={"investigation_status": "OPEN"})
    assert result.status_code == 200 and result.json() == before


@pytest.mark.parametrize("data", [
    {}, {"investigation_status": "RESOLVED"}, {"investigation_status": "open"},
    {"investigation_status": None}, {"investigation_status": True},
    {"investigation_status": "CLOSED", "risk_score": 0},
    {"investigation_status": "CLOSED", "transaction_id": "other"},
    {"investigation_status": "CLOSED", "card_token": "private-card-token"},
])
def test_invalid_patch_cannot_change_alert_or_echo_sensitive_values(environment, data, caplog):
    e = environment
    alert_id, _ = create_alert(e)
    before = e.client.get(f"{ALERTS}/{alert_id}").json()
    result = e.client.patch(f"{ALERTS}/{alert_id}", json=data)
    assert result.status_code == 422
    assert "private-card-token" not in result.text and "private-card-token" not in caplog.text
    assert e.client.get(f"{ALERTS}/{alert_id}").json() == before


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": "not-an-int"},
    {"investigation_status": "private-device-token"},
])
def test_list_invalid_pagination_or_status_is_422(environment, params):
    result = environment.client.get(ALERTS, params=params)
    assert result.status_code == 422
    assert "private-device-token" not in result.text


@pytest.mark.parametrize("method", ["get", "patch"])
def test_missing_alert_returns_404(environment, method):
    request = getattr(environment.client, method)
    kwargs = {"json": {"investigation_status": "CLOSED"}} if method == "patch" else {}
    result = request(f"{ALERTS}/missing", **kwargs)
    assert result.status_code == 404 and result.json() == {"detail": "Alert not found"}


def test_failure_after_all_inserts_rolls_back_alert_and_entire_scoring_unit(environment, monkeypatch, caplog):
    e = environment

    def fail(db):
        db.flush()
        # The real inserts occurred, but none may survive the failed commit.
        for model in (Transaction, GraphSyncJob, IdempotencyRecord, FraudAlert):
            assert db.scalar(select(func.count()).select_from(model)) == 1
        raise OperationalError("private SQL card-device-token", {}, RuntimeError("private credentials"))

    with monkeypatch.context() as patch:
        patch.setattr(e.sessions.class_, "commit", fail)
        result = e.client.post(URL, json=payload(), headers={"Idempotency-Key": "atomic"})
    assert result.status_code == 500
    assert "private" not in result.text and "private" not in caplog.text
    assert counts(e) == (0, 0, 0) and alert_count(e) == 0
    create_alert(e, key="atomic")
    assert counts(e) == (1, 1, 1) and alert_count(e) == 1


def test_failed_alert_update_rolls_back_and_sanitizes_error(environment, monkeypatch, caplog):
    e = environment
    alert_id, _ = create_alert(e)
    before = e.client.get(f"{ALERTS}/{alert_id}").json()

    def fail(db):
        db.flush()
        raise OperationalError("private SQL device-token", {}, RuntimeError("private credentials"))

    with monkeypatch.context() as patch:
        patch.setattr(e.sessions.class_, "commit", fail)
        result = e.client.patch(f"{ALERTS}/{alert_id}", json={"investigation_status": "CLOSED"})
    assert result.status_code == 500 and result.json() == {"detail": "Alert database operation failed"}
    assert "private" not in result.text and "private" not in caplog.text
    assert e.client.get(f"{ALERTS}/{alert_id}").json() == before


@pytest.mark.parametrize("route", [ALERTS, ALERTS + "/anything"])
def test_alert_read_database_errors_are_sanitized(environment, monkeypatch, route, caplog):
    e = environment

    def fail(*args, **kwargs):
        raise OperationalError("private SQL card-token", {}, RuntimeError("private credentials"))

    monkeypatch.setattr(e.sessions.class_, "_execute_internal", fail)
    result = e.client.get(route)
    assert result.status_code == 500 and result.json() == {"detail": "Alert database operation failed"}
    assert "private" not in result.text and "private" not in caplog.text


def test_openapi_limits_patch_and_documents_alert_response(environment):
    document = environment.client.get("/openapi.json").json()
    assert set(document["paths"][ALERTS]) == {"get"}
    assert set(document["paths"][ALERTS + "/{alert_id}"]) == {"get", "patch"}
    schema = document["components"]["schemas"]["AlertUpdate"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"investigation_status"}
    assert schema["properties"]["investigation_status"]["enum"] == ["OPEN", "IN_REVIEW", "CLOSED"]
