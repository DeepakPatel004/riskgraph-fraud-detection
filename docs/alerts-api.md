# Phase 5.1: fraud alerts and investigation status

An alert is an item for human review, **not proof of fraud**. Each newly scored
HIGH transaction creates exactly one alert using the existing model's saved
classification. LOW transactions and legacy `POST /api/v1/transactions` ingestion
create none. Risk thresholds, model artifacts, historical cutoff rules, graph
freshness requirements and Neptune semantics are unchanged.

## Creation, retries and storage

The existing `fraud_alerts` table already has all required fields, a foreign key
to the transaction, and a unique constraint on `transaction_id`. No migration,
new table, database reset, or historical backfill is required. Existing records
are preserved, including any previously scored HIGH transactions without alerts;
this phase creates alerts only during a new successful scored submission.

The transaction is flushed first so its foreign-key children can be inserted.
The alert, transaction, recorded score/features, graph outbox job, and optional
idempotency key then commit together. A failed insert/commit rolls all of them
back. An unavailable graph/model produces no alert and no fallback score.

An identical keyed scoring retry returns the original saved score without
inference or creating another alert. It also preserves subsequent investigation
changes: replay never reopens a CLOSED alert. Conflicting keys and duplicate
transaction IDs retain their existing 409 behavior.

An alert copies the score and factual indicator codes from the scoring result.
Indicators describe historical observations; they do not explain the model's
internal decision or confirm a fraud cause. See [indicator rules](scoring-api.md#classification-and-observed-indicators).

## Investigation status

| Status | Meaning |
|---|---|
| `OPEN` | Newly created and awaiting review. |
| `IN_REVIEW` | An operator is examining it. |
| `CLOSED` | The investigation is finished. This does not mean confirmed fraud. |

PATCH accepts exactly one required field, `investigation_status`, with one of
these three uppercase values. Any status can change to any other status, including
reopening; this first version has no resolution categories, roles or transition
approval workflow. A real status change automatically updates `updated_at` in
UTC. Sending the current status again is a no-op, preserving `updated_at`.
`created_at`, transaction ID, score and indicators are never changed by PATCH.

## API examples

List newest alerts, with bounded offset pagination and an optional status filter:

```http
GET /api/v1/alerts?limit=20&offset=0&investigation_status=OPEN
```

`limit` defaults to 20 and must be 1–100. `offset` defaults to 0 and must be
nonnegative. `total` counts all alerts matching the filter, before pagination.
An empty page returns `items: []`. Equal creation times are ordered by descending
alert ID for a stable tie-breaker. Pages can shift if new alerts arrive between
requests; there is no snapshot/cursor pagination in this version.

Illustrative response (the score is an example, not a performance claim):

```json
{
  "items": [{
    "alert_id": "b3824b76-761c-42e3-a533-7307b104855b",
    "transaction_id": "payment-123",
    "risk_score": 0.85,
    "risk_indicators": ["MULTIPLE_CARD_TRANSACTIONS_24H"],
    "investigation_status": "OPEN",
    "created_at": "2026-10-12T12:00:00Z",
    "updated_at": "2026-10-12T12:00:00Z"
  }],
  "total": 1,
  "limit": 20,
  "offset": 0
}
```

```http
GET /api/v1/alerts/b3824b76-761c-42e3-a533-7307b104855b

PATCH /api/v1/alerts/b3824b76-761c-42e3-a533-7307b104855b
Content-Type: application/json

{"investigation_status": "IN_REVIEW"}
```

GET detail and PATCH both return the single alert object shown in `items` above,
with the saved/current status and timestamps. Successful reads and updates return
200. There is no standalone alert creation endpoint and no scoring response change.

| Error | Response |
|---|---|
| Invalid pagination, filter, PATCH status, missing status, or extra PATCH field | 422 with a validation `detail` list; rejected values/context are omitted. |
| Missing alert on GET or PATCH | 404, `{"detail": "Alert not found"}`. |
| Alert query/update database failure | 500, `{"detail": "Alert database operation failed"}`; SQL/credentials are omitted and failed writes roll back. |

## Local security and frontend contract

**No API authentication or authorization is implemented. These endpoints are for
a restricted local demo only. Do not expose them publicly without access control.**
Every caller able to reach this demo can read and update all alerts. API access
control, transport security, operator audit history, and protected deployment
configuration are prerequisites for a shared/public deployment; they are outside
this phase. Neptune IAM configuration is not authentication for these HTTP APIs.

Alert responses deliberately expose no card/device tokens, full transaction
features, model objects or SQL. Existing transaction endpoints have their own
contracts and still need deployment access restrictions. Keep payment tokens and
credentials out of alert indicator strings and operational logs.

Phase 6.4's Investigations UI loads the paginated collection, opens alerts by ID,
and PATCHes a new status after explicit confirmation for CLOSED, then refreshes
list/detail. See [the frontend guide](../frontend/README.md) for deep links,
privacy filtering and uncertain-write handling. A PATCH returns the saved state.
Concurrent operator updates use last-write-wins; there is no
optimistic version check or operator change history yet. Investigation updates
use SQL only and do not call the model, Neptune, or graph synchronization jobs.

Normal scoring still requires the verified Neptune population and existing
single-host ingestion policy. Real Neptune verification remains pending. Alert
reads/updates work independently of graph/model availability. Tests use temporary
SQLite databases and fake graph/model dependencies; they require no AWS access.

Phase 5.1 verification passed **459 tests, 0 failed**, including 30 new cases and
extended existing scoring tests. Coverage includes threshold boundaries, unique
alerts, retries, status/timestamp changes, filters, pagination, failed inserts
and commits, safe errors, and legacy compatibility. The existing Starlette/httpx
test-client warning remains. SHA-256 checks confirmed that the application SQLite
database and all six model artifacts were unchanged. No dependencies were added.
