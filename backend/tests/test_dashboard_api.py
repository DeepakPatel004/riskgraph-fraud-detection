"""SQL-only dashboard contract tests with isolated temporary SQLite files."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, get_db, make_engine
from app.main import app
from app.models import FraudAlert, GraphSyncJob, IdempotencyRecord, Transaction
from app.services.dashboard_service import (
    DashboardService, MAX_TREND_DAYS, alert_counts_query, daily_counts_query,
    transaction_counts_query, trend_dates,
)


ROOT = "/api/v1/dashboard"
PATHS = ["summary", "risk-distribution", "transaction-trends", "alert-status"]
PRIVATE = "private-card-device-token-credential"
TIME = datetime(2026, 10, 10, 5, tzinfo=timezone.utc)


@pytest.fixture
def dashboard():
    parent = Path(__file__).resolve().parents[2] / "data/test-dashboard-api"
    root = parent / uuid4().hex
    root.mkdir(parents=True)
    engine = make_engine(f"sqlite:///{(root / 'source.db').as_posix()}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_db():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(engine=engine, sessions=sessions, client=client)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
        assert root.resolve().parent == parent.resolve()
        shutil.rmtree(root)


def insert(e, identifier, *, score=None, level=None, timestamp=TIME, processing_status="RECEIVED", alert=None):
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp)
    with e.sessions() as db:
        db.add(Transaction(transaction_id=identifier, amount=Decimal("100.00"), currency="USD",
                           card_token=PRIVATE, device_token=PRIVATE, event_time=timestamp,
                           risk_score=score, risk_level=level, processing_status=processing_status,
                           features_json={"private": PRIVATE}))
        db.flush()
        if alert is not None:
            db.add(FraudAlert(transaction_id=identifier, risk_score=score,
                              investigation_status=alert, risk_indicators=["TEST"]))
        db.commit()


def snapshot(e):
    with e.sessions() as db:
        return [db.execute(select(model.__table__).order_by(*model.__table__.primary_key)).all()
                for model in (Transaction, FraudAlert, GraphSyncJob, IdempotencyRecord)]


def test_empty_database_returns_zero_counts_and_empty_unanchored_trend(dashboard):
    e = dashboard
    assert e.client.get(ROOT + "/summary").json() == {
        "total_transactions": 0, "scored_transactions": 0, "unscored_transactions": 0,
        "high_risk_transactions": 0, "low_risk_transactions": 0,
        "total_alerts": 0, "open_alerts": 0, "in_review_alerts": 0, "closed_alerts": 0,
    }
    assert e.client.get(ROOT + "/risk-distribution").json() == {"LOW": 0, "HIGH": 0, "UNSCORED": 0}
    assert e.client.get(ROOT + "/alert-status").json() == {"OPEN": 0, "IN_REVIEW": 0, "CLOSED": 0}
    assert e.client.get(ROOT + "/transaction-trends").json() == {"start_date": None, "end_date": None, "items": []}


def test_summary_distributions_and_alert_totals_use_saved_state_without_join_inflation(dashboard):
    e = dashboard
    for status in ("OPEN", "IN_REVIEW", "CLOSED"):
        insert(e, status, score=0.01, level="HIGH", alert=status)
    insert(e, "low-1", score=0.99, level="LOW", processing_status="SCORED")
    insert(e, "low-2", score=0, level="LOW", processing_status="RECEIVED")
    insert(e, "unscored-1")
    insert(e, "unscored-2", processing_status="SCORED")
    summary = e.client.get(ROOT + "/summary")
    assert summary.status_code == 200
    assert summary.json() == {
        "total_transactions": 7, "scored_transactions": 5, "unscored_transactions": 2,
        "high_risk_transactions": 3, "low_risk_transactions": 2,
        "total_alerts": 3, "open_alerts": 1, "in_review_alerts": 1, "closed_alerts": 1,
    }
    risk = e.client.get(ROOT + "/risk-distribution").json()
    assert risk == {"LOW": 2, "HIGH": 3, "UNSCORED": 2}
    assert sum(risk.values()) == summary.json()["total_transactions"]
    alerts = e.client.get(ROOT + "/alert-status").json()
    assert alerts == {"OPEN": 1, "IN_REVIEW": 1, "CLOSED": 1}
    assert sum(alerts.values()) == summary.json()["total_alerts"]


@pytest.mark.parametrize("level", [None, "LOW", "HIGH"])
def test_null_score_is_unscored_even_with_leftover_label(dashboard, level):
    e = dashboard
    insert(e, "unscored", level=level)
    assert e.client.get(ROOT + "/risk-distribution").json() == {"LOW": 0, "HIGH": 0, "UNSCORED": 1}
    summary = e.client.get(ROOT + "/summary").json()
    assert summary["scored_transactions"] == summary["low_risk_transactions"] == summary["high_risk_transactions"] == 0
    assert summary["unscored_transactions"] == summary["total_transactions"] == 1


@pytest.mark.parametrize("level", [None, "MEDIUM"])
@pytest.mark.parametrize("path", ["summary", "risk-distribution"])
def test_nonnull_score_without_supported_saved_classification_is_not_silently_guessed(dashboard, level, path):
    e = dashboard
    insert(e, "inconsistent", score=0.8, level=level)
    result = e.client.get(ROOT + "/" + path)
    assert result.status_code == 500
    assert result.json()["error"] == {"code": "DASHBOARD_DATA_INCONSISTENT", "retryable": False}
    assert PRIVATE not in result.text


@pytest.mark.parametrize("path", ["summary", "alert-status"])
def test_unknown_alert_status_is_not_hidden_from_totals(dashboard, path):
    e = dashboard
    insert(e, "inconsistent", score=0.8, level="HIGH", alert="UNSUPPORTED")
    result = e.client.get(ROOT + "/" + path)
    assert result.status_code == 500 and result.json()["error"]["code"] == "DASHBOARD_DATA_INCONSISTENT"


def test_zero_alert_categories_and_closed_investigations_do_not_change_risk_counts(dashboard):
    e = dashboard
    insert(e, "T1", score=0.9, level="HIGH", alert="OPEN")
    insert(e, "older-high-without-alert", score=0.9, level="HIGH")
    assert e.client.get(ROOT + "/alert-status").json() == {"OPEN": 1, "IN_REVIEW": 0, "CLOSED": 0}
    with e.sessions() as db:
        alert_id = db.scalar(select(FraudAlert.alert_id))
    assert e.client.patch(f"/api/v1/alerts/{alert_id}", json={"investigation_status": "CLOSED"}).status_code == 200
    assert e.client.get(ROOT + "/alert-status").json() == {"OPEN": 0, "IN_REVIEW": 0, "CLOSED": 1}
    summary = e.client.get(ROOT + "/summary").json()
    assert summary["high_risk_transactions"] == 2 and summary["total_alerts"] == 1


def test_trends_use_utc_event_dates_inclusive_end_and_zero_days(dashboard):
    e = dashboard
    for i, timestamp in enumerate([
        "2026-09-30T23:59:59.999999Z", "2026-10-01T00:00:00Z",
        "2026-10-02T01:30:00+02:00", "2026-10-02T05:30:00+05:30",
        "2026-10-02T23:59:59.999999Z", "2026-10-03T00:00:00Z",
    ]):
        insert(e, f"T{i}", timestamp=timestamp)
    result = e.client.get(ROOT + "/transaction-trends", params={"start_date": "2026-10-01", "end_date": "2026-10-04"})
    assert result.status_code == 200
    assert result.json() == {"start_date": "2026-10-01", "end_date": "2026-10-04", "items": [
        {"date": "2026-10-01", "transaction_count": 2}, {"date": "2026-10-02", "transaction_count": 2},
        {"date": "2026-10-03", "transaction_count": 1}, {"date": "2026-10-04", "transaction_count": 0},
    ]}
    one_day = e.client.get(ROOT + "/transaction-trends", params={"start_date": "2026-10-02", "end_date": "2026-10-02"}).json()
    assert one_day["items"] == [{"date": "2026-10-02", "transaction_count": 2}]


def test_default_window_anchors_to_latest_event_not_clock_or_created_time(dashboard):
    e = dashboard
    insert(e, "latest", timestamp="2020-02-29T23:59:59Z")
    insert(e, "outside", timestamp="2020-01-30T23:59:59Z")
    insert(e, "first", timestamp="2020-01-31T00:00:00Z")
    result = e.client.get(ROOT + "/transaction-trends").json()
    assert result["start_date"] == "2020-01-31" and result["end_date"] == "2020-02-29"
    assert len(result["items"]) == 30
    assert result["items"][0]["transaction_count"] == result["items"][-1]["transaction_count"] == 1
    assert sum(row["transaction_count"] for row in result["items"]) == 2


@pytest.mark.parametrize("params,start,end,length", [
    ({"start_date": "2026-10-01"}, "2026-10-01", "2026-10-30", 30),
    ({"end_date": "2026-10-30"}, "2026-10-01", "2026-10-30", 30),
    ({"start_date": "2026-10-01", "end_date": "2026-10-03"}, "2026-10-01", "2026-10-03", 3),
    ({"end_date": "0001-01-01"}, "0001-01-01", "0001-01-01", 1),
])
def test_explicit_and_partial_empty_windows_return_zero_dates(dashboard, params, start, end, length):
    result = dashboard.client.get(ROOT + "/transaction-trends", params=params)
    assert result.status_code == 200, result.text
    assert result.json()["start_date"] == start and result.json()["end_date"] == end
    assert len(result.json()["items"]) == length
    assert all(row["transaction_count"] == 0 for row in result.json()["items"])


def test_maximum_inclusive_window_is_allowed_and_bounded(dashboard):
    e = dashboard
    insert(e, "first", timestamp="2026-01-01T00:00:00Z")
    insert(e, "last", timestamp="2026-03-31T23:59:59.999999Z")
    result = e.client.get(ROOT + "/transaction-trends", params={"start_date": "2026-01-01", "end_date": "2026-03-31"})
    assert result.status_code == 200, result.text
    assert len(result.json()["items"]) == MAX_TREND_DAYS == 90
    assert result.json()["items"][0]["transaction_count"] == result.json()["items"][-1]["transaction_count"] == 1
    assert sum(row["transaction_count"] for row in result.json()["items"]) == 2


@pytest.mark.parametrize("params", [
    {"start_date": "2026-10-03", "end_date": "2026-10-01"},
    {"start_date": "2026-01-01", "end_date": "2026-04-01"},
    {"start_date": "2020-01-01", "end_date": "2030-01-01"},
    {"start_date": PRIVATE}, {"end_date": "2026-02-30"},
    {"start_date": "9999-12-12"},
    {"start_date": "9999-12-30", "end_date": "9999-12-31"},
])
def test_invalid_or_excessive_windows_are_structured_422_without_queries(dashboard, params):
    e = dashboard
    statements = []

    def track(*args):
        statements.append(args[2])

    event.listen(e.engine, "before_cursor_execute", track)
    try:
        result = e.client.get(ROOT + "/transaction-trends", params=params)
    finally:
        event.remove(e.engine, "before_cursor_execute", track)
    assert result.status_code == 422
    assert result.json()["error"] == {"code": "VALIDATION_ERROR", "retryable": False}
    assert PRIVATE not in result.text and not statements


@pytest.mark.parametrize("path", PATHS)
def test_database_failures_are_sanitized_unavailable(dashboard, monkeypatch, caplog, path):
    e = dashboard

    def fail(*args, **kwargs):
        raise OperationalError("SELECT private SQL " + PRIVATE, {}, RuntimeError(PRIVATE))

    monkeypatch.setattr(e.sessions.class_, "_execute_internal", fail)
    result = e.client.get(ROOT + "/" + path)
    assert result.status_code == 503
    assert result.json() == {"detail": "Dashboard database is unavailable",
                             "error": {"code": "DASHBOARD_DATABASE_UNAVAILABLE", "retryable": True}}
    assert result.headers["Retry-After"] == "2"
    assert PRIVATE not in result.text and PRIVATE not in caplog.text


@pytest.mark.parametrize("path", PATHS)
def test_unexpected_internal_failures_are_sanitized_500(dashboard, monkeypatch, caplog, path):
    e = dashboard

    def fail(*args, **kwargs):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(e.sessions.class_, "_execute_internal", fail)
    result = e.client.get(ROOT + "/" + path)
    assert result.status_code == 500
    assert result.json() == {"detail": "Dashboard request failed",
                             "error": {"code": "DASHBOARD_FAILED", "retryable": False}}
    assert PRIVATE not in result.text and PRIVATE not in caplog.text


def test_analytics_are_read_only_sql_aggregates_without_neptune_or_inference(dashboard, monkeypatch):
    from app.graph.neptune_client import NeptuneClient
    from app.services.fraud_inference_service import FraudInferenceService
    from app.services import fraud_inference_service

    e = dashboard
    insert(e, "T1", score=0.9, level="HIGH", alert="OPEN")
    before = snapshot(e)
    statements = []

    def forbidden(*args, **kwargs):
        pytest.fail("Dashboard attempted writes, graph access or model inference/loading")

    def track(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    with monkeypatch.context() as patch:
        patch.setattr(e.sessions.class_, "commit", forbidden)
        patch.setattr(e.sessions.class_, "flush", forbidden)
        patch.setattr(NeptuneClient, "execute", forbidden)
        patch.setattr(FraudInferenceService, "predict", forbidden)
        patch.setattr(fraud_inference_service, "get_fraud_inference_service", forbidden)
        patch.setattr("app.api.graph_dependencies.build_graph_population", forbidden)
        event.listen(e.engine, "before_cursor_execute", track)
        try:
            for path in PATHS:
                result = e.client.get(ROOT + "/" + path)
                assert result.status_code == 200, result.text
                assert PRIVATE not in result.text
        finally:
            event.remove(e.engine, "before_cursor_execute", track)
    assert len(statements) == 5  # Summary/risk/status each one; default trend MAX + counts.
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert all("count(" in statement.lower() or "max(" in statement.lower() for statement in statements)
    assert before == snapshot(e)


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])
def test_aggregate_queries_compile_portably_with_aware_utc_bounds(dialect):
    queries = [transaction_counts_query(), alert_counts_query(),
               daily_counts_query(trend_dates(date(2026, 10, 1), date(2026, 10, 2)))]
    for query in queries:
        compiled = query.compile(dialect=dialect)
        text = str(compiled).lower()
        assert "count(" in text and "case when" in text
        assert "date_trunc" not in text and "strftime" not in text and "extract(" not in text
        for value in compiled.params.values():
            if isinstance(value, datetime):
                assert value.utcoffset() == timedelta(0)


def test_dashboard_import_and_requests_do_not_load_optional_ml_or_neptune_modules():
    # Fresh process proves success without previously cached/imported ML modules.
    script = """
import sys
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core.database import Base, get_db, make_engine
from app.main import app
engine = make_engine('sqlite://', poolclass=StaticPool)
Base.metadata.create_all(engine)
sessions = sessionmaker(bind=engine, autoflush=False)
def db():
    with sessions() as session:
        yield session
app.dependency_overrides[get_db] = db
with TestClient(app) as client:
    for path in ('summary', 'risk-distribution', 'transaction-trends', 'alert-status'):
        assert client.get('/api/v1/dashboard/' + path).status_code == 200
assert not any(name in sys.modules for name in ('xgboost', 'sklearn', 'botocore',
    'app.ml.fraud_predictor', 'app.services.fraud_inference_service', 'app.graph.neptune'))
engine.dispose()
"""
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_legacy_and_alert_endpoints_keep_previous_response_contracts(dashboard):
    e = dashboard
    data = {"transaction_id": "legacy", "amount": "10.00", "card_token": "demo",
            "event_time": TIME.isoformat()}
    created = e.client.post("/api/v1/transactions", json=data)
    assert created.status_code == 201 and created.json()["risk_score"] is None
    assert e.client.get("/api/v1/transactions/legacy").json() == created.json()
    assert e.client.get("/api/v1/transactions").json()["total"] == 1
    assert e.client.get("/api/v1/alerts").json() == {"items": [], "total": 0, "limit": 20, "offset": 0}
    assert e.client.get("/health").status_code == 200
    assert e.client.get(ROOT + "/risk-distribution").json() == {"LOW": 0, "HIGH": 0, "UNSCORED": 1}


def test_openapi_documents_all_four_typed_get_responses_and_date_parameters(dashboard):
    document = dashboard.client.get("/openapi.json").json()
    models = {"summary": "DashboardSummary", "risk-distribution": "RiskDistribution",
              "alert-status": "AlertStatusCounts", "transaction-trends": "TransactionTrends"}
    for path, model in models.items():
        operation = document["paths"][ROOT + "/" + path]
        assert set(operation) == {"get"}
        assert operation["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/" + model)
    params = document["paths"][ROOT + "/transaction-trends"]["get"]["parameters"]
    assert {param["name"] for param in params} == {"start_date", "end_date"}
