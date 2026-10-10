# Neptune Database adapter: offline implementation

The adapter implements the existing synchronous GraphRepository contract. It is
not wired into FastAPI, does not create infrastructure, and is never instantiated
at import time. Transaction APIs and SQL storage remain unchanged.

## Transport and explicit construction

NeptuneClient uses the existing HTTPX dependency for HTTPS, HTTP/1.1, verified TLS,
timeouts, and form encoding. Its default client ignores proxy environment settings;
custom HTTP clients can be injected. Injected clients remain caller-owned. Close
owned clients with `close()` or use the client as a context manager.

Configuration fields are `neptune_endpoint` (hostname only), `neptune_port` (8182),
`neptune_timeout` (10 seconds), `neptune_iam_auth_enabled` (true), and
`neptune_region`. The endpoint and region default to unset. Use the explicit factory
`NeptuneClient.from_settings(settings)` and pass that client to
`NeptuneGraphRepository(client)` only when deliberately enabling graph access.
The commented `.env.example` entries are placeholders, not live configuration.

The wire format is:

```text
POST https://<hostname>:<port>/openCypher
Content-Type: application/x-www-form-urlencoded
Accept: application/json

query=<URL-encoded Cypher>&parameters=<URL-encoded JSON object>
```

For instance, the decoded fields could be `query=RETURN $value AS value` and
`parameters={"value":"sample"}`. JSON serialization happens before URL encoding.
Amounts are Decimal-compatible strings, not JSON floating point. No transaction
values are interpolated into Cypher. LIMIT clauses use the fixed literal 100 for
pages (1 for root lookup), avoiding dependence on non-static limit expressions.
This is the Neptune Database `/openCypher` form endpoint, not Bolt or the separate
lowercase `/opencypher` JSON Data API route.

HTTP failures expose status codes without response bodies. Network failures,
timeouts, invalid JSON (including appended streaming errors), invalid envelopes,
malformed nodes/edges, and invalid pagination cause explicit, sanitized errors.
The code does not log tokens, credentials, request bodies, or database responses.
It does not follow redirects or automatically retry: a timed-out mutation may have
committed even when the client did not receive its result.

## IAM signing

IAM-enabled requests use botocore's SigV4Auth with service `neptune-db` and the
configured cluster region. The signer signs the exact encoded bytes sent to HTTPX.
The standard credential chain is resolved lazily, and frozen credentials are
obtained per request so refreshable session credentials can be used. No access
keys or secret keys belong in RiskGraph configuration.

`backend/requirements-neptune.txt` declares optional `botocore>=1.35,<2`. It was
not installed for this milestone. Normal operation/tests need only the existing
requirements. A real IAM-enabled request requires installing that optional file
later; disabled authentication and injected test signers require no AWS SDK.
No credential provider or metadata service is called during normal tests.

## Graph storage and queries

Node IDs are a label prefix plus a SHA-256 hash of the domain identifier. Edge IDs
hash source ID, relationship kind, and target ID. Node labels are Transaction,
Card and Device; relationships are USES_CARD and USES_DEVICE. Labels and domain
identifiers cannot collide even if a token equals a transaction ID.

Each upsert is one mutation query: MERGE the transaction by custom ID, set selected
properties, delete only obsolete card/device edges, and MERGE the current token
nodes and deterministic edges. The no-device variant omits Device creation.
Retries with identical data keep the same IDs. No Identity nodes, labels for fraud
outcomes, predictions, or raw card-number fields are introduced. Callers must
supply pseudonymous tokens. Unused old token nodes may remain in storage; they
are unreachable from transactions and do not affect the public network/features.

Requires Neptune Database engine >=1.2.0.2 for custom IDs. Neptune enforces ID
uniqueness, but MERGE alone does not make concurrent updates safe. Concurrent
writers can get duplicate-data or transaction-conflict errors, or overwrite each
other. Serialize updates per transaction ID and plan explicit reconciliation and
retry policy before production. The transport deliberately does not hide failures.

Historical reads match only the requested Card or Device's transactions, bounded
by `[cutoff - 7 days, cutoff)` in UTC. Fixed-width ISO UTC strings with six fractional
digits make string comparison chronological without relying on Neptune datetime
functions. All adapter writes use this format. Existing external graph data using
different timestamp formats must be migrated before using these queries.

Each result page contains at most 100 nodes, sorted and keyset-paginated by
transaction ID. Counts, unique tokens, and Decimal totals are accumulated in
Python. The 24-hour lower bound is also inclusive. Exact-time and future events are
excluded. Missing histories yield zero; absent devices yield zero device features.
Mixed currencies in the card's 7-day history raise ValueError. Pagination scans
all relevant history rather than silently truncating financial features. Runtime
and distinct-token memory still grow with that history.

Network retrieval performs deterministic breadth-first traversal with individual
one-hop queries. Neighbors are ordered by `(kind, identifier)`, with selected IDs
excluded; no variable-length paths are used. The caller's positive limit counts
all nodes, including the root. A final paginated query gets only edges between
selected nodes and returns them in the in-memory adapter's order. Unknown roots
return an empty GraphNetwork. Investigation traversal has no historical cutoff.

Multiple HTTP reads do not share a database snapshot. Matching in-memory behavior
assumes stable graph data during the operation. Before concurrent production use,
define ingestion/read consistency and historical snapshot strategy. Performance
under high-degree tokens and large selected-ID lists also needs measurement.

## What offline tests establish

HTTPX MockTransport tests inspect method, URL, form encoding, parameters, signing
handoff, timeouts, safe errors, and malformed responses. A stateful fake handles
operation markers and returns Neptune-shaped JSON. Shared graph contract tests run
against both the in-memory adapter and this fake-backed HTTP adapter; additional
tests exercise histories and traversal beyond a 100-row page.

The fake is NOT a Cypher parser. Query-generation assertions check intended text
and parameters; they do not prove the database executes the text. Fake botocore
modules check credential refresh and `neptune-db`/region signing wiring, not the
cryptographic signature implementation. No real Neptune calls were made.

Pending real integration checks:

- Run generated write/read queries against the selected Neptune engine version,
  especially OPTIONAL MATCH + DELETE + MERGE and null device parameters.
- Verify real SigV4 requests, session tokens, credential refresh, TLS, and VPC access.
- Verify custom-ID conflicts, rollback of a failing single-query mutation, and
  behavior after a timeout with an unknown commit outcome.
- Confirm response shapes, fixed-width timestamp ordering, Decimal string storage,
  UTF-8 ordering, and paginated results on real data.
- Measure query plans, high-degree traversal, history size, and concurrent writes/
  reads before enabling the adapter in an API or worker.

## AWS references checked for this implementation

- [HTTPS endpoint and JSON response format](https://docs.aws.amazon.com/neptune/latest/userguide/access-graph-opencypher-queries.html)
- [Parameterized query encoding](https://docs.aws.amazon.com/neptune/latest/userguide/opencypher-parameterized-queries.html)
- [Supported clauses/functions](https://docs.aws.amazon.com/neptune/latest/userguide/feature-opencypher-compliance.html)
- [Custom node and edge IDs](https://docs.aws.amazon.com/neptune/latest/userguide/access-graph-opencypher-extensions.html)
- [Why IDs help prevent duplicates](https://docs.aws.amazon.com/neptune/latest/userguide/best-practices-content-15.html)
- [Python IAM authentication](https://docs.aws.amazon.com/neptune/latest/userguide/iam-auth-connecting-python.html)
- [Neptune query rewrites and unsupported FOREACH](https://docs.aws.amazon.com/neptune/latest/userguide/migration-opencypher-rewrites.html)
