# Phase 4.2: scored transaction API and atomic persistence

`POST /api/v1/transactions/score` uses the existing `FraudScoringRequest` and
read-only orchestrator, then saves a prediction and an outbox job. No transaction
is inserted into Neptune before scoring. No alerts or investigation features
are implemented. Scoring is disabled by default, with no AWS calls at import.

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
  "graph_sync_status": "PENDING"
}
```

New submissions return 201; identical keyed retries return 200. Retries return
the saved score/model version and the **current** graph sync status, which can
change after worker delivery. Duplicate IDs, conflicting keys and late events
return 409. Invalid input is 422, with rejected input values removed from scoring
validation errors. Busy ingestion, incomplete/unknown graph history and model
unavailability return 503. Unexpected database/server failures return sanitized
500 errors. GET-by-ID and the legacy creation route retain their response schemas.

## Sequence and idempotency

1. Validate scoring input through FastAPI.
2. Acquire the common ingestion gate. Resolve idempotency and duplicate IDs.
3. Reject event times earlier than any accepted SQL transaction.
4. End the SQL read transaction before loading/scoring dependencies.
5. The orchestrator checks SQL freshness and makes one historical graph lookup,
   using `[event_time-window, event_time)`. All exact-time and future events are
   excluded. The SQL checker closes its own session before graph/model calls.
6. Save the transaction with `processing_status=SCORED`, score/level, and JSON
   metadata; flush the parent, add one outbox job and optional idempotency record,
   then commit once. Any failed insertion rolls everything back.
7. Return the committed prediction and graph state; release the gate.

`features_json.fraud_scoring` stores versioned source categories, model version
and historical feature/cutoff metadata. Tokens remain only in existing transaction
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
The complete suite passed 393 tests. A separate local API smoke check used the
unchanged real model with an explicitly isolated empty SQL/graph fixture: it
returned 201 with score `0.24356554448604584` (LOW), then an identical keyed replay
returned 200 with one saved job. External network access was blocked during those
requests. This is an execution check, not a new performance evaluation.

Fail-closed means uncertainty produces an error, never an invented score.
Atomic persistence means the transaction, job and key all commit or all roll back.
Idempotency means a retry recovers one saved result. Request concurrency means
multiple callers overlap; the gate permits only one ingestion workflow at a time.

Remaining live verification includes Neptune query/visibility behavior, IAM,
network timeouts, real outbox delivery/recovery, operator reconciliation and
deployment lock/filesystem assumptions. This task made no AWS connection and
implemented no Phase 4.3 investigation features.

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
