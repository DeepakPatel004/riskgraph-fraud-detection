# Transactional graph synchronization

## Outbox and data preservation

`create_transaction` inserts the SQL transaction, flushes the parent row, then
inserts exactly one GraphSyncJob and the optional idempotency record. One commit
makes all records durable together. A failure rolls all of them back. Existing
idempotent replays and duplicate transaction IDs do not insert more jobs. The
job table's unique transaction_id constraint is an additional safeguard.

The HTTP endpoint never calls a graph adapter. It returns the existing response
fields, initially including graph_sync_status=PENDING. processing_status remains
RECEIVED; this milestone does not run fraud scoring. Subsequent reads may report
PROCESSING, SYNCED or FAILED for graph synchronization.

## Additive migration

The only schema addition is nullable `graph_sync_jobs.lease_token VARCHAR(36)`.
Existing next_attempt_at doubles as lease expiry while a job is PROCESSING; no
extra timestamp column is needed. Existing retry data, rows, indexes and unique/
foreign-key constraints remain intact. Old rows receive NULL for the lease token.

For an existing installation, stop API/workers, take a backup, then run from backend:

```powershell
python -m scripts.migrate_graph_sync_leases
```

The migration checks the schema and is safe to rerun. It never drops tables,
reinitializes the database, or rewrites records. Run only one migration process.
Fresh `python -m scripts.init_db` installations already have the column; create_all
alone does not upgrade older tables. The same additive ALTER TABLE is compatible
with PostgreSQL, but no live PostgreSQL migration test has been performed.

Historical transactions without jobs are not automatically backfilled. This
milestone queues new transactions only; a separate, deliberate backfill is needed
if an older installation contains such records. The local database was empty
before migration.

## State and retry policy

| State | Meaning | next_attempt_at |
|---|---|---|
| PENDING | Waiting for initial delivery or retry | Earliest claim time |
| PROCESSING | A worker owns a time-limited lease | Lease expiry |
| SYNCED | Graph call succeeded and SQL completion committed | Completion time; not scheduled |
| FAILED | Retry budget exhausted | Failure time; not scheduled |

Each committed claim increases attempts, including claims followed by a crash.
A failed SQL claim does not consume an attempt. Defaults are 5 maximum attempts,
initial delay 5 seconds, multiplier 2, and delay cap 300 seconds. Failures normally
wait 5, 10, 20, 40 seconds between the five attempts. The cap matters for custom
larger attempt budgets. Delay is measured from failure completion, in aware UTC.

Failure preserves the parent transaction. Job and parent graph_sync_status are
updated together in the same SQL transaction. Only fixed error summaries are
stored/logged; adapter exception text, token values and credentials are omitted.
Success clears last_error and the lease token. FAILED jobs require operator action.

## Claiming, I/O and recovery

The service selects a due PENDING job or an expired PROCESSING job. PostgreSQL
uses FOR UPDATE SKIP LOCKED. A conditional UPDATE checks the candidate's previous
status, attempts, lease token and due time on both backends. Only a successful
claimant may continue. SQLite is supported with ONE worker; its limited concurrent
write support can produce lock errors under contention. Such failed claims must
not call the graph, and the continuous worker polls again later.

A claim commits PROCESSING, a new random lease token, an incremented attempt
count, and lease expiry (default 300 seconds). Claim one job just before processing,
not an entire batch at once. Then a separate short read session copies SQL data
into a TransactionNode and checks ownership again. All SQL sessions are closed
before GraphRepository.upsert_transaction is called. An empty device token from
the existing API is treated as absent during graph conversion.

Completion opens a new SQL transaction and updates only when the lease token still
matches. Old owners cannot overwrite a newer owner's success or retry state.
If a graph write succeeds but SQL completion fails, the durable PROCESSING lease
remains. After expiry the write is retried idempotently. An expired lease at the
attempt limit becomes FAILED rather than staying permanently stuck. A job may be
FAILED even if the graph contains its data when completion repeatedly fails;
manual verification/retry is then needed.

This is bounded at-least-once delivery with a terminal failure state, not
exactly-once delivery. A process can crash after writing the graph but before
recording success, so the same write can run again. Graph adapters must keep
upserts idempotent.

Leases cannot distinguish a dead process from a very slow process. A late graph
call can overlap a new attempt after lease expiry. SQL fencing prevents stale
completion, but cannot cancel/fence an already-running Neptune request. Configure
leases comfortably longer than adapter execution time, serialize writes per
transaction, and verify timeout/concurrency policy before production. No heartbeat
or distributed graph-write fencing is implemented here. Workers need reasonably
synchronized UTC clocks. Multi-worker PostgreSQL behavior still needs live tests.

## Starting and stopping the worker

The default adapter setting is disabled. Importing worker modules does not create
graph connections. Normal tests use injected in-memory repositories or fake HTTP.

From backend, with the existing virtual environment active, an explicit local demo is:

```powershell
python -m app.workers.graph_sync_worker --adapter memory --allow-ephemeral --once
python -m app.workers.graph_sync_worker --adapter memory --allow-ephemeral --batch-size 20 --poll-interval 1
```

Use a disposable development/test database for memory demos. Memory mode is NOT
persistent graph storage: restart loses nodes while SQL jobs remain SYNCED, so
they are not automatically replayed. The acknowledgement flag and warning prevent
silently treating this adapter as production storage. A persistent local graph
adapter is not part of this milestone.

`--once` handles one batch. Without it, the worker polls continuously. Ctrl+C or
SIGTERM requests shutdown, finishes the active job, and stops claiming new jobs.
Forced termination or an interrupt during a write relies on lease recovery.
An adapter/network call must eventually return or time out for graceful shutdown
to finish. A persistence error exits nonzero in once mode; continuous mode logs a
safe message, waits, and polls again.

The explicit future Neptune command is `python -m app.workers.graph_sync_worker
--adapter neptune`. It requires the configuration and optional IAM dependency in
the Neptune guide and WILL contact the configured cluster when jobs are due.
It was NOT run for this milestone. No AWS resources or credentials were used.

Worker settings in .env (all optional, names shown with defaults):

```text
GRAPH_SYNC_ADAPTER=disabled
GRAPH_SYNC_INITIAL_DELAY=5
GRAPH_SYNC_BACKOFF_FACTOR=2
GRAPH_SYNC_MAX_DELAY=300
GRAPH_SYNC_MAX_ATTEMPTS=5
GRAPH_SYNC_LEASE_SECONDS=300
GRAPH_SYNC_POLL_INTERVAL=1
GRAPH_SYNC_BATCH_SIZE=20
```

## Manually retrying a terminal failure

Find the job ID with a local SQL query that selects only operational fields:

```sql
SELECT job_id, transaction_id, attempts
FROM graph_sync_jobs WHERE status = 'FAILED';
```

Then run:

```powershell
python -m app.workers.graph_sync_worker --retry-failed <job-id>
```

This performs no graph call. It conditionally resets only FAILED jobs to PENDING,
clears the error/lease, resets attempts to zero, schedules now, and updates the
parent status atomically. It does not steal active leases or reset SYNCED jobs.
Then start the appropriate worker deliberately. Repeated manual resets explicitly
grant new retry budgets; they are not an automatic infinite retry loop.

## Verification limits

Tests cover atomic inserts, no duplicate jobs on replay, actual graph nodes/edges,
retry boundaries/caps, sanitized errors, concurrent SQLite claims using separate
connections, expired/stale leases, SQL completion/update/commit failures, worker
shutdown/polling, API contracts, manual retries and legacy migration preservation.
PostgreSQL locking SQL is compiled offline; no live PostgreSQL or Neptune calls
were made. Scaling, clock skew, concurrent real Neptune writes, and real database
outages remain deployment integration checks. No new dependencies were added.
