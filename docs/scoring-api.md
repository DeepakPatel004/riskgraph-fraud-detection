# Scored transaction API: Phase 4.3 reliability contract

`POST /api/v1/transactions/score` uses the existing `FraudScoringRequest` and
read-only orchestrator, then saves a prediction and an outbox job. No transaction
is inserted into Neptune before scoring. Phase 5.1 now creates one SQL alert for
each new HIGH result in the same commit; LOW results create none. See
[alert API](alerts-api.md) for investigation status endpoints. Scoring is still
disabled by default, with no AWS calls at import.

## Request and response

```http
POST /api/v1/transactions/score
Content-Type: application/json
Idempotency-Key: checkout-123

{
  "transaction_id": "payment-123",
  "amount": "100.00",
  "currency": "USD",
  "card_token": "pseudonymous-card",
  "device_token": null,
  "event_time": "2026-10-10T05:00:00Z",
  "ProductCD": null,
  "M1": null
}
```

ProductCD and M1–M9 are optional strict strings/nulls. Blank device tokens become
null. Amount must be positive; currency must explicitly be USD; timestamps must
include a timezone and are normalized to UTC. No categories are fabricated.

An illustrative response using the previously verified empty-history model
sample is below; actual scores depend on the supplied historical features:

```json
{
  "transaction_id": "payment-123",
  "risk_score": 0.24356554448604584,
  "risk_level": "LOW",
  "model_version": "xgb-v1-3393fbaabb6c045a04db6d08a58d382934c5364333186cc7c33a033efd3104b7",
  "graph_sync_status": "PENDING",
  "risk_indicators": []
}
```

New submissions return 201; identical keyed retries return 200. Retries return
the saved score/model version and the **current** graph sync status, which can
change after worker delivery. Duplicate IDs, conflicting keys and late events
return 409. Invalid input is 422, with rejected input values removed from scoring
validation errors. Busy ingestion, incomplete/unknown graph history and model
unavailability return 503. Unexpected database/server failures return sanitized
500 errors. GET-by-ID and the legacy creation route retain their response schemas.

## Classification and observed indicators

The saved model threshold is the single classification source: **HIGH** when
`risk_score >= threshold`, **LOW** otherwise. The existing bundle's threshold is
`0.7617719769477844`; runtime reads it from the verified artifacts, not this
document. The same inference result supplies both SQL and the API response.
There is no MEDIUM category. A model score is an estimate, and HIGH is a threshold
flag, not proof of fraud; LOW does not prove that a transaction is legitimate.

`risk_indicators` is one additive response field containing these stable codes
in the listed order when their rules match:

| Code | Observed condition, excluding the current transaction |
|---|---|
| `MULTIPLE_CARD_TRANSACTIONS_24H` | At least two prior card transactions in 24 hours. |
| `MULTIPLE_DEVICE_TRANSACTIONS_24H` | At least two prior device transactions in 24 hours. |
| `CARD_MULTIPLE_DEVICES_7D` | Card associated with at least two distinct devices in seven days. |
| `DEVICE_MULTIPLE_CARDS_7D` | Device associated with at least two distinct cards in seven days. |
| `AMOUNT_AT_LEAST_3X_CARD_AVERAGE_7D` | Current amount is at least three times a positive seven-day card average. |

These are factual rule-based signals from verified historical aggregates. They
do not explain the trees' internal decision, imply statistical significance, or
establish a fraud cause. The three-times rule is a simple descriptive convention,
not a tuned model threshold. Amount comparisons preserve Decimal precision.
Indicators can appear with LOW scores, and HIGH scores can have no indicators.
Zero/absent history cannot trigger the amount comparison, and missing device
history creates no device signal. Unavailable graph history still blocks scoring.

The pure indicator component performs no I/O and never changes the score or level.
New results store the indicator list and rule version in existing JSON metadata,
so keyed retries return the recorded observations without graph/model access.
Older saved scores lacking this metadata return an empty list: no indicators
were recorded, rather than proof that no risk signals existed. No migration or
historical backfill is needed.

## Structured errors

Scoring errors retain their existing `detail` string (or validation error list)
and add a consistent machine-readable `error` object. For example:

```json
{
  "detail": "Historical graph is unavailable or incomplete",
  "error": {"code": "GRAPH_UNAVAILABLE", "retryable": true}
}
```

| HTTP | Error codes | Retryable |
|---|---|---|
| 422 | `VALIDATION_ERROR` | No; correct the request. |
| 409 | `DUPLICATE_TRANSACTION`, `IDEMPOTENCY_CONFLICT`, `LATE_EVENT` | No; resolve the conflict. |
| 503 | `GRAPH_FRESHNESS_UNAVAILABLE`, `GRAPH_UNAVAILABLE`, `MODEL_UNAVAILABLE` | Yes, after the dependency recovers. |
| 503 | `INGESTION_BUSY` | Yes; also returns `Retry-After: 1`. |
| 503 | `SCORING_UNAVAILABLE` | No automatic retry; deployment configuration needs attention. |
| 500 | `PERSISTENCE_ERROR`, `INCONSISTENT_STATE`, `SCORING_FAILED` | No automatic retry; inspect the server issue first. |

Retryable means the same request may succeed after recovery, not that immediate
retries are guaranteed to work. Missing/corrupt artifacts and terminal graph jobs
may require operator repair. Reuse the same idempotency key after any uncertain
outcome. Dependency exception text, SQL, rejected input values, credentials and
stack traces are omitted. Errors do not produce fallback scores or indicators.
Legacy transaction endpoints keep their previous error envelope.

## Sequence and idempotency

1. Validate scoring input through FastAPI.
2. Acquire the common ingestion gate. Resolve idempotency and duplicate IDs.
3. Reject event times earlier than any accepted SQL transaction.
4. End the SQL read transaction before loading/scoring dependencies.
5. The orchestrator checks SQL freshness and makes one historical graph lookup,
   using `[event_time-window, event_time)`. All exact-time and future events are
   excluded. The SQL checker closes its own session before graph/model calls.
6. Save the transaction with `processing_status=SCORED`, score/level, and JSON
   metadata; flush the parent, add one outbox job, optional idempotency record,
   and one OPEN alert if the saved level is HIGH, then commit once. Any failed
   insertion rolls everything back. Alert indicators match the saved result.
7. Return the committed prediction and graph state; release the gate.

`features_json.fraud_scoring` stores versioned source categories, model version,
historical feature/cutoff metadata and recorded indicators with their rule version.
Tokens remain only in existing transaction
columns, not duplicated in this JSON or the scoring response. No additional
transaction columns are required.

The keyed request hash includes all normalized scoring fields and a scoring-
specific operation prefix. It cannot be confused with the legacy endpoint's hash.
Matching committed retries bypass the graph/model factory entirely, including
when those dependencies are offline. A failed request creates no successful
idempotency record. A duplicate ID without a matching key is 409, not a rerun.
Integrity errors are 409 only when a conflicting key/ID is verified after rollback;
unrelated integrity failures stay 500. If a response is lost after commit, retry
with the same key to recover the committed result.

## Freshness: which graph is authoritative?

Production scoring supports one **local, file-backed SQLite source on one host**,
one configured API worker, and a dedicated persistent Neptune graph. PostgreSQL,
SQLite memory/URI sources, multiple hosts and network filesystems are not supported
for scoring in this prototype. Legacy API functionality is still available.

An operator must first establish all of these conditions, outside request handling:

- The SQL database is the complete authoritative transaction population for this
  graph. No external/direct SQL writers bypass the ingestion policy.
- The Neptune endpoint is dedicated to that source, with no unrelated nodes,
  external graph writers, or unreconciled restores/deletions.
- Existing data is reconciled, and API/worker configuration names the same graph
  epoch. Never set the verification flag merely because a graph responds or is empty.
- Only immutable transaction outbox deliveries modify graph transaction data.
  Graph or SQL restore/replacement, or source relocation, requires a new population name,
  reconciliation and fresh acknowledgements before scoring resumes.

Opt-in settings (not enabled by this task) are `FRAUD_SCORING_ENABLED=true`,
`SCORING_API_WORKERS=1`, `GRAPH_SYNC_ADAPTER=neptune`, Neptune settings,
`GRAPH_POPULATION_NAME=<dedicated-epoch>` and `GRAPH_POPULATION_VERIFIED=true`.
The verification flag is an operator attestation of these preconditions, not an
automatic completeness proof. There is no environment switch for test memory
graphs. Offline tests use explicit dependency overrides with controlled fixtures.

Both the default scoring adapter and worker derive the same population digest
from the canonical SQLite path, Neptune hostname/port and operator epoch. A worker
records that digest in `graph_sync_jobs.graph_population_id` only after successful
graph delivery. Memory/unidentified workers record null, which cannot certify
history. Workers must use the same configured adapter, source and epoch as the API;
the CLI adapter override does not substitute for shared production configuration.

While holding the ingestion gate, the checker inspects every relevant earlier
transaction in the seven-day card **or device** population. Missing jobs, null or
different population IDs, PENDING/PROCESSING/FAILED jobs, inconsistent transaction
status, an outstanding lease, or non-USD history prevent scoring. A newer synced
timestamp never overrides an older incomplete job. Outside the gate, or without
verified population identity, freshness is UNKNOWN. Empty in-memory state alone
proves nothing. A real graph query failure never becomes zero history.

Old SYNCED rows with null population IDs remain untrusted. Reconciliation/replay
must establish delivery to the chosen graph before accepting them; the migration
does not relabel old acknowledgements. No automated reconciliation tool is added.

## Ordering and concurrency policy

All API creation requests use a common gate keyed by the canonical SQLite file.
It combines a thread lock and a nonblocking operating-system file lock. Another
request/process on the same host receives retryable 503 while ingestion is busy.
Locks release on process exit. This is stronger than a process-local mutex, but
is **not** a multi-host coordination protocol. `SCORING_API_WORKERS` must be 1;
it is a deployment declaration, not discovery of the actual uvicorn worker count.
The OS lock additionally excludes cooperating processes using the same source.

The gate covers ordering checks, inference and SQL commit; no SQL transaction is
held open during Neptune/model work. Graph workers can deliver jobs concurrently:
relevant incomplete jobs block scoring, and acknowledged immutable deliveries are
safe to repeat. Exact-time deliveries cannot enter the exclusive cutoff window.
The gate prevents a new API source event from arriving between the freshness
check and commit. Other ingestion paths/direct writers are unsupported.

New scored events cannot precede the latest accepted SQL event. Once a scored
transaction exists, new legacy submissions cannot precede the latest scored
event either; keyed replays remain allowed. This is the deliberate safety
restriction added to legacy writes after scoring starts. Before scoring starts,
the existing INR defaults, payload and chronological flexibility are unchanged.
Equal timestamps remain allowed and exclude one another from history, per the
existing model contract. A later relevant request may receive 503 until the prior
outbox job is delivered. There is no automatic waiting, quarantine or rescore.

## Additive migration and verification

Stop API/workers, back up SQLite, then run from `backend/`:

```powershell
.\.venv\Scripts\python.exe -m scripts.migrate_graph_population
```

The repeatable migration adds nullable `graph_sync_jobs.graph_population_id`
of type `VARCHAR(64)` and preserves every existing table/row. Fresh databases get it from
the existing SQLAlchemy metadata. No drop/recreate, backfill or dataset processing
is performed. The existing local database was backed up under ignored
`artifacts/db_backups/`, migrated and checked for preserved rows and foreign keys.

The additive ALTER syntax is also suitable for PostgreSQL, but has not been
live-tested there. PostgreSQL scoring is intentionally refused until an equivalent
source/ingestion coordination policy is implemented and verified.

The suite uses fake inference, controlled graph adapters and real local SQLite.
It checks atomic rollback/retry, single lookups/jobs, freshness/population state,
late events, same-time exclusion, cross-process locks, HTTP errors, compatibility
and migration preservation. No regular test requires AWS or large datasets.
Phase 4.2 passed 393 tests. A separate local API smoke check used the
unchanged real model with an explicitly isolated empty SQL/graph fixture: it
returned 201 with score `0.24356554448604584` (LOW), then an identical keyed replay
returned 200 with one saved job. External network access was blocked during those
requests. This is an execution check, not a new performance evaluation.

Fail-closed means uncertainty produces an error, never an invented score.
Atomic persistence means the transaction, job, key and any HIGH alert all commit
or all roll back.
Idempotency means a retry recovers one saved result. Request concurrency means
multiple callers overlap; the gate permits only one ingestion workflow at a time.

Remaining live verification includes Neptune query/visibility behavior, IAM,
network timeouts, real outbox delivery/recovery, operator reconciliation and
deployment lock/filesystem assumptions. This task made no AWS connection and
implemented no investigation or case-management features.

Phase 4.3 integration tests exercise the API, real temporary SQLite persistence,
and controlled dependencies together. They cover scores immediately below/at/above
the saved threshold, recorded indicators/replay, structured errors, real loader
failures and sensitive-data redaction. Earlier cutoff, concurrency, rollback and
legacy compatibility tests remain part of the full suite. The classifier and
application database are unchanged, and normal scoring remains disabled until
the graph population is verified.

Phase 4.3 verification: **429 tests passed, 0 failed** (36 new cases). The only
warning is the existing Starlette/httpx test-client deprecation. SHA-256 checks
confirmed all six model artifact files and the application SQLite database were
unchanged. No dependencies or database migrations were added.

## Files changed in Phase 4.2

```text
.env.example
.gitignore
backend/app/api/transactions.py
backend/app/api/scoring_dependencies.py
backend/app/core/config.py
backend/app/main.py
backend/app/models/entities.py
backend/app/schemas/fraud_scoring.py
backend/app/services/graph_population.py
backend/app/services/ingestion_gate.py
backend/app/services/scored_transaction_service.py
backend/app/services/sql_graph_freshness.py
backend/app/services/graph_sync_service.py
backend/app/services/transaction_service.py
backend/app/workers/graph_sync_worker.py
backend/scripts/migrate_graph_population.py
backend/tests/test_scoring_api.py
docs/fraud-scoring.md
docs/phase-4-inference-contract.md
docs/scoring-api.md
```
