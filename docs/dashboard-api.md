# Phase 5.3: dashboard analytics APIs

These read-only endpoints summarize existing SQL records. They do not score or
reclassify transactions, load XGBoost artifacts, query Neptune, create alerts,
or modify stored data. No new tables, migrations, dependencies, or infrastructure
are needed. Examples below are illustrative, not model performance claims.

## Endpoints and responses

```http
GET /api/v1/dashboard/summary
```

```json
{
  "total_transactions": 7,
  "scored_transactions": 5,
  "unscored_transactions": 2,
  "high_risk_transactions": 3,
  "low_risk_transactions": 2,
  "total_alerts": 3,
  "open_alerts": 1,
  "in_review_alerts": 1,
  "closed_alerts": 1
}
```

| Metric | Meaning |
|---|---|
| `total_transactions` | All SQL transaction records, regardless of processing/sync state. |
| `scored_transactions` | Transactions with a non-null stored `risk_score`. |
| `unscored_transactions` | Transactions with a null `risk_score`. |
| `high_risk_transactions` | Non-null score and saved `risk_level=HIGH`. |
| `low_risk_transactions` | Non-null score and saved `risk_level=LOW`. |
| `total_alerts` | All actual SQL alert records; not a recount of HIGH transactions. |
| `open_alerts` | Saved investigation status `OPEN`. |
| `in_review_alerts` | Saved investigation status `IN_REVIEW`. |
| `closed_alerts` | Saved investigation status `CLOSED`. |

`processing_status` and `graph_sync_status` do not determine score classification.
A null score is always UNSCORED, even if an old/inconsistent row has a leftover
LOW/HIGH label. A non-null score without a supported saved LOW/HIGH label returns
a controlled data-consistency error instead of silently guessing a class. An
unsupported stored alert status likewise returns a consistency error.

Successful responses obey:

```text
total_transactions = scored_transactions + unscored_transactions
scored_transactions = high_risk_transactions + low_risk_transactions
total_alerts = open_alerts + in_review_alerts + closed_alerts
```

HIGH means the saved model classification flagged a transaction for review,
not proven fraud; LOW is not proof of legitimacy. Closing an alert does not
change the transaction's saved score or level. Older HIGH transactions may have
no alert because Phase 5.1 intentionally did not backfill them.

```http
GET /api/v1/dashboard/risk-distribution
```

```json
{"LOW": 2, "HIGH": 3, "UNSCORED": 2}
```

These use exactly the summary's persisted classification rules. Null scores are
never grouped into LOW. Values sum to `total_transactions` for the same database
state. No threshold comparison or model inference runs during analytics.

```http
GET /api/v1/dashboard/alert-status
```

```json
{"OPEN": 1, "IN_REVIEW": 1, "CLOSED": 1}
```

All three keys are always present, including zero-count statuses. This counts
alert rows only and never creates duplicates or joins in extra transaction rows.

## Daily transaction trends

```http
GET /api/v1/dashboard/transaction-trends?start_date=2026-10-01&end_date=2026-10-03
```

```json
{
  "start_date": "2026-10-01",
  "end_date": "2026-10-03",
  "items": [
    {"date": "2026-10-01", "transaction_count": 2},
    {"date": "2026-10-02", "transaction_count": 0},
    {"date": "2026-10-03", "transaction_count": 1}
  ]
}
```

Trends use **event_time**, not ingestion `created_at`. Each point counts all
transactions in the UTC interval `[date 00:00, next date 00:00)`. An event exactly
at midnight belongs to the new day; input offsets have already been normalized
by the existing UTC storage type. Returned dates are ordered ascending. Gaps
appear with zero counts, and both requested calendar dates are included.

| Parameters | Window policy |
|---|---|
| Both dates | Inclusive requested dates, at most **90 days**. |
| Only `start_date` | 30 days forward, including start. |
| Only `end_date` | Up to 30 days backward, including end. |
| Neither | 30 days ending on the latest stored transaction's UTC event date. |

Dates use `YYYY-MM-DD`. The default is deterministic for unchanged data and
does not depend on today's clock, which is useful with historical demo data.
Backward windows clip at the earliest supported date `0001-01-01`. Forward
calendar overflow returns 422. `end_date=9999-12-31` is rejected because the
exclusive next-midnight boundary cannot be represented by Python datetime.
Reversed ranges and ranges over 90 days inclusive return 422 before counting.

For an empty database, summary/risk/alert counts are zeros. Trends with no dates
return `{"start_date": null, "end_date": null, "items": []}` because no latest
event exists. An explicitly or partially specified valid window still returns
its daily zero-count points.

## SQL aggregation and compatibility

`DashboardService` performs `COUNT(CASE WHEN ... THEN 1 END)` aggregations.
COUNT ignores the implicit NULL else branch, and naturally returns zero for
an empty table. Only aggregate values reach Python; no full transaction or
alert ORM result sets are loaded.

Summary joins two **one-row aggregate subqueries** in one statement, avoiding
row multiplication from joining raw transactions and alerts. Risk distribution
and alert status each use one aggregate query. Explicit/partial trend windows
use one statement with at most 90 conditional daily counts; default trends first
query `MAX(event_time)` to determine the window. Python constructs only the
bounded date list and pairs it with SQL counts, including zero-count days.

Daily buckets compare timestamps against aware UTC midnight boundaries and use
an overall range predicate compatible with the existing `event_time` index.
There is no SQLite `strftime`, PostgreSQL `date_trunc`, or timezone-sensitive
database date extraction. Existing `UTCDateTime` stores/binds UTC appropriately
for both engines and interprets legacy naive SQLite values as UTC.

SQLite execution is covered by tests; the expressions also compile for
PostgreSQL, but this phase does **not** claim live PostgreSQL verification.
Up to 90 conditional counts trade some database CPU for simple portable SQL;
large production workloads still need query-plan/load measurement. Summary
counts intentionally cover the whole SQL dataset, not only the trend window.

Separate HTTP requests can observe different database states while transactions
or investigation statuses change. There is no dashboard-wide snapshot, cache,
streaming update, or cross-request consistency guarantee. A default trend's
latest-event lookup and aggregate are also separate statements; it is a recent
window view, not a distributed snapshot.

## Error responses

Errors use the existing structured scoring/explorer format. For example:

```json
{
  "detail": "Dashboard database is unavailable",
  "error": {"code": "DASHBOARD_DATABASE_UNAVAILABLE", "retryable": true}
}
```

| HTTP | Code | Meaning |
|---|---|---|
| 422 | `VALIDATION_ERROR` | Invalid date syntax, reversed/overlong window, or unsupported calendar bound. Correct the request. |
| 503 | `DASHBOARD_DATABASE_UNAVAILABLE` | SQL operation failed; retry after database recovery. `Retry-After: 2`. |
| 500 | `DASHBOARD_DATA_INCONSISTENT` | Unsupported saved classification/status needs operator inspection. No automatic retry. |
| 500 | `DASHBOARD_FAILED` | Unexpected internal failure. No automatic retry. |

The 503 policy applies to SQLAlchemy failures; some require operator repair
rather than an immediate retry. Errors omit SQL statements, database/credential
details, stack traces, and rejected query input values. Failed requests do not
return fabricated zero metrics. Existing endpoint error contracts are unchanged.

## Frontend integration and security

Phase 6.2's Overview page displays summary cards, a Recharts UTC activity line,
and count-based risk/status bar charts. It keeps UNSCORED separate and displays
“No transaction history” for an empty unanchored trend. Open investigations
means `open_alerts` (OPEN only, excluding IN_REVIEW). Refresh is manual, without
polling. Each section handles its own failure; failed calls never become zero
metrics. See [the frontend guide](../frontend/README.md) for UI behavior and
the existing transaction APIs used by the transaction table.

**API authentication/authorization is not implemented. These endpoints are for a
restricted local demo only and must not be exposed publicly without access control.**
Although responses contain no payment tokens or model internals, aggregate
transaction volumes and investigation counts are still business-sensitive.
Neptune IAM does not protect these HTTP routes. Authenticated/authorized access,
transport protection and deployment safeguards remain future work.

Dashboard requests work independently of graph population verification, AWS,
model artifacts, and the scoring enablement flag because they read SQL only.
This does not weaken scoring or graph-explorer safety checks.

Phase 5.3 verification passed **551 tests, 0 failed**, including 40 new cases.
Tests cover totals/categories, status updates, UTC/date boundaries, zero days,
maximum ranges, safe failures, read-only aggregation, and no graph/ML calls.
A fresh-process test verifies that analytics requests do not load optional ML
modules. SQLite execution and PostgreSQL SQL compilation pass; no live PostgreSQL
or AWS connection was used. SHA-256 checks confirmed all six model artifacts and
the application SQLite database were unchanged. The existing Starlette/httpx
test-client deprecation warning remains.
