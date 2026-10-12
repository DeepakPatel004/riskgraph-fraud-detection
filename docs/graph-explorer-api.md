# Phase 5.2: transaction graph explorer

This read-only endpoint returns a connected network for a later React/Cytoscape.js
view. It does not score transactions, create alerts, repair graph data, or run
graph synchronization. No schema migration or dependency installation is needed.

## Request and response

```http
GET /api/v1/transactions/TXN101/graph?limit=50
```

`limit` defaults to 50 and accepts integers from 1 to 100. It counts **all nodes**,
including the requested transaction, cards and devices, rather than only related
transactions. The response uses deterministic, namespaced opaque node IDs.

This illustrative response uses `limit=1`, showing only the starting transaction:

```json
{
  "transaction_id": "TXN101",
  "nodes": [{
    "id": "Transaction:fbebb29c92ced38655a32b5c9f9b0e72d578b5d6be1aec740b4aa14c76c5fb39",
    "type": "Transaction",
    "label": "TXN101",
    "properties": {"amount": "150.00", "currency": "USD"}
  }],
  "edges": [],
  "node_count": 1,
  "edge_count": 0,
  "truncated": null
}
```

IDs are `kind:SHA256(source identifier)`.
`node_count` and `edge_count` always match the returned arrays. Edges have the form
`{"source": "Transaction:<hash>", "target": "Card:<hash>", "type": "USES_CARD"}`,
where both IDs must exist in the returned nodes. Device edges use `USES_DEVICE`.
An edge's original transaction-to-card/device direction is preserved.

| Node type | Label | Properties |
|---|---|---|
| `Transaction` | Transaction ID | Amount as an exact two-decimal string, currency. |
| `Card` | `Card ` followed by the first eight hash characters | Empty object. |
| `Device` | `Device ` followed by the first eight hash characters | Empty object. |

Full node IDs use the entire SHA-256 hash, not the abbreviated label. Different
node types have separate namespaces. No raw card/device tokens, event times,
fraud labels, scores, model objects, or arbitrary transaction JSON are serialized.
Transaction IDs remain visible as the requested ID and transaction labels.

## Traversal and limits

Graph traversal means following links from a starting node. The existing
`GraphRepository.get_transaction_network` uses **breadth-first search (BFS)**:
visit the starting transaction, then its immediate card/device neighbors, then
their neighbors, expanding outward in layers. Relationships are traversed in
both directions to find shared-card and shared-device transactions. Neighbors
are visited in `(kind, original identifier)` order. The API preserves BFS node
order and sorts edges by their displayed source ID, type, and target ID.

The API makes one bounded repository network call. It neither adds unbounded
Cypher paths nor changes the repository's traversal contract. Neptune's existing
adapter pages fixed-size queries internally; the in-memory adapter retains its
existing whole-memory adjacency scan and is for small local fixtures.

`truncated` is **false** when fewer nodes than the limit are returned: the
repository exhausted the visible connected component. It is **null** when the
node limit is reached: the current contract does not report whether additional
nodes exist. This includes a component whose size equals the limit exactly.
The API does not invent `true`/`false` or fetch extra nodes to guess. The frontend
should show “Node limit reached; more nodes may exist” for null.

With `limit=1`, a seed-only result is expected and is not evidence of isolation.
Below the cap, a seed missing its mandatory card (or known device) relationship
is inconsistent and returns 503. Transactions with no device legitimately have
only a card relationship. All returned nodes must be connected and all edges
must reference returned nodes with the correct relationship/token association.

This investigation view has **no historical cutoff** and may contain later
transactions. It must never be reused as the fraud model's historical features.

## SQL and population safety

Before constructing/querying the graph:

1. Find the requested SQL transaction; otherwise return 404, even if graph
   configuration is absent.
2. Require transaction `graph_sync_status=SYNCED`, an existing SYNCED outbox job,
   no active lease, and a recorded graph population identity. PENDING/PROCESSING
   is retryable unavailable; FAILED needs operator recovery.
3. End the SQL read transaction before graph I/O. Lazily obtain a verified
   `GraphPopulation`, and compare its identity with the acknowledged job.

Production wiring reuses the existing cached Neptune repository constructor and
`neptune_population_id`: identity binds the SQLite source path, endpoint, port
and operator population epoch. It requires `graph_sync_adapter=neptune`, a
configured endpoint/population name, `graph_population_verified=true`, file-based
SQLite, and the existing one-worker policy. It refuses memory/disabled adapters
in production. Tests inject a verified in-memory or mock-HTTP population explicitly.

The explorer does not require `fraud_scoring_enabled` or model artifacts. It
cannot use an unverified population merely because scoring is disabled. No
Neptune client, credentials lookup, or external request occurs at module import.

After reading, validate the root, node bound, relationships, and connectedness.
Check every returned transaction against SQL again, requiring matching source
attributes, current sync acknowledgement, and population identity. A missing root,
foreign graph transaction, wrong source data, or invalid network is unavailable;
an empty graph is never presented as a valid synchronized transaction.

These checks are not a distributed snapshot. Concurrent changes after checking
can still occur, and Neptune traversal uses several queries. The verified flag
is operator attestation, not an automatic completeness proof. A bounded result
and `truncated=false` describe the **currently visible graph component**, not a
guarantee that every SQL transaction has reached Neptune: pending/new neighbors
may be absent. Real Neptune visibility, synchronization and restore/reconciliation
procedures still require deployment verification. Do not enable the verified
flag to bypass that work. PostgreSQL source-population coordination remains pending.

## Structured errors

Errors use the existing scoring-style envelope, for example:

```json
{
  "detail": "Transaction graph is unavailable or unverified",
  "error": {"code": "GRAPH_SYNC_PENDING", "retryable": true}
}
```

| HTTP | Code | Action |
|---|---|---|
| 404 | `TRANSACTION_NOT_FOUND` | Correct the requested transaction ID. |
| 422 | `VALIDATION_ERROR` | Correct limit; rejected input/context is omitted. |
| 503 | `GRAPH_SYNC_PENDING` | Retry after worker progress; `Retry-After: 2`. |
| 503 | `GRAPH_UNAVAILABLE` | Retry after graph/network recovery; `Retry-After: 2`. |
| 503 | `GRAPH_SYNC_FAILED` | Operator must retry/recover the failed job. |
| 503 | `GRAPH_SYNC_UNVERIFIED`, `GRAPH_POPULATION_UNVERIFIED`, `GRAPH_POPULATION_MISMATCH` | Reconcile SQL acknowledgements/configured population. |
| 503 | `GRAPH_CONFIGURATION_UNAVAILABLE` | Correct graph/runtime configuration. |
| 503 | `GRAPH_INCONSISTENT` | Investigate missing/invalid graph data; no automatic repair. |
| 500 | `GRAPH_EXPLORER_FAILED` | Inspect internal/SQL failure; no automatic retry. |

Only PENDING/PROCESSING and graph/network dependency errors are marked retryable.
Other errors need corrected input or operator action. Raw dependency exceptions,
AWS details, SQL, tokens and stack traces are never returned or logged by this
endpoint. Legacy transaction/alert and scoring response contracts stay unchanged.

## Frontend and security limitations

Graph visualization turns these nodes into displayed shapes and edges into links.
Cytoscape.js can use each node as `{data: {id, label, type, ...properties}}` and
each edge as `{data: {source, target, type}}`. Styling can distinguish node types;
the API's opaque IDs let edges consistently reference the correct shapes.
Fetch only on demand, handle 503 separately from 404, and explain a null
truncation flag instead of claiming the whole network is visible. No React
implementation or dashboard analytics are included in this phase.

**Authentication/authorization is not implemented. This is a restricted local
demo endpoint and must not be exposed publicly without access control.**
Hashes are stable pseudonyms, not encryption or anonymization. Guessable tokens
can be dictionary-matched and repeated IDs reveal associations across responses.
Transaction IDs, amounts and relationship structure are also sensitive. Public
deployment needs authenticated, authorized access and appropriate retention and
logging controls; hashing alone is insufficient.

No AWS resources or live Neptune calls were used for this phase. Tests use
temporary SQLite and fake graph dependencies, including Neptune's real adapter
over mock HTTP. GET requests perform no graph or SQL writes and no inference.

Phase 5.2 verification: **511 tests passed, 0 failed**, including 52 new cases.
The existing Starlette/httpx test-client deprecation warning remains. SHA-256
checks confirmed that the application SQLite database and all six model artifact
files were unchanged. No migration, model training, or dependency installation
was performed.
