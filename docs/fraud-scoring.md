# Phase 4.1: read-only fraud scoring orchestration

`FraudScoringService` coordinates existing graph history and model inference.
The orchestrator performs no SQL write, graph write, alert creation or sync-job
creation. Phase 4.2 adds the separate [public persistence workflow](scoring-api.md).
No model is loaded by constructing the scoring service.

## Request and response

`app.schemas.fraud_scoring.FraudScoringRequest` accepts these flat fields:

| Fields | Contract |
|---|---|
| `transaction_id` | Required string, 1–64 letters/digits/underscores/hyphens. |
| `amount` | Required positive finite Decimal, Numeric(18,2) compatible. |
| `currency` | Required literal `USD`; unsupported currencies fail validation. |
| `card_token` | Required pseudonymous string, 1–128 characters, not whitespace-only. |
| `device_token` | Optional pseudonymous string, up to 128 characters; blank/whitespace-only becomes null. |
| `event_time` | Required timezone-aware datetime, normalized to UTC. |
| `ProductCD`, `M1`–`M9` | Optional strict strings or null, using existing categorical validation. |

Missing categorical fields stay null. Blank categories, the internal missing
marker, numbers, booleans, containers and unexpected fields are rejected.
Nonblank tokens are preserved exactly; this service does not silently change
graph identities by trimming them. Requests are frozen and revalidated on entry
because `model_copy()` can bypass normal Pydantic validation.

`FraudScoringResult` returns `transaction_id`, `risk_score`, `risk_level`,
`model_version` and `historical_features`. Historical metadata contains the five
actual graph aggregates, UTC `cutoff`, `window_start_24h`, `window_start_7d`,
`boundary_policy="[start, end)"`, `currency="USD"` and `freshness="VERIFIED"`.
No tokens, categorical payloads or internal estimator objects appear in results.
The model's saved threshold and preprocessing are reused unchanged.

## Service flow and dependency injection

The constructor accepts a `GraphRepository`, existing `FraudInferenceService`,
and optional `GraphFreshnessChecker`. Dependency injection simply means passing
these collaborators into the service rather than having it construct hidden SQL,
AWS or model clients. Tests supply small fakes; future FastAPI startup can pass
the existing cached inference service and a configured graph adapter.

Orchestration means coordinating steps, with each collaborator retaining its job:

1. Revalidate the scoring request.
2. Enter the injected freshness guard for this exact request.
3. Refuse unless it returns `GraphFreshness.FRESH` (not a string or boolean).
4. Query `get_historical_features(card_token, device_token, event_time)` once.
5. Validate the returned feature types and build historical metadata.
6. Map amount/currency, `device_token is None`, and the ten categorical values
   into `InferenceTransaction`; pass that and `GraphFeatures` to inference.
7. Exit the guard successfully, then return the typed result.

No lookup happens for rejected freshness. A permitted attempt makes exactly one
lookup, with no retry or zero-history fallback. Inference checks aggregate logical
consistency as before. The current transaction is never upserted by this service.
The graph repository owns elapsed `[cutoff-window, cutoff)` semantics: include
the lower boundary, exclude every exact-time/future event; use no labels/scores.

## Explicit freshness contract

Graph freshness means the graph contains the relevant earlier source transactions
needed for this request, rather than merely being reachable. The protocol is:

```python
def guard(request: FraudScoringRequest) -> ContextManager[GraphFreshness]: ...
```

The guard yields `FRESH`, `STALE`, or `UNKNOWN`. `FRESH` attests to completeness
of the relevant seven-day card/device history at the request's event-time cutoff,
USD card amount history, and consistent worker state. The attestation must remain
valid through the graph read; a guard must not suppress errors. The implementation
must correspond to the injected graph and source, not an unrelated repository.

**A timestamp watermark alone is insufficient.** A newer synchronized transaction
does not prove that an older relevant transaction is synchronized. A persistent
implementation must account for relevant missing, pending/retrying, processing
(including expired leases), and failed jobs. A graph write followed by a failed SQL
acknowledgement is still uncertain until reconciled. Changes to associations,
late-arriving events and concurrent workers also require a coherent read policy.
Scope both card and device histories: a transaction on another card can still
contribute device features.

Phase 4.1 only defines this injectable contract. It does not implement SQL checks,
worker coordination, locking or a distributed snapshot. The built-in
`UnknownGraphFreshnessChecker` always yields `UNKNOWN`, so production scoring
is disabled until a trustworthy implementation is supplied. Tests attest only
to their complete, controlled fixtures; an unconditional FRESH checker is not a
production solution. A context-manager interface allows future coordination to
cover the read instead of a racy standalone check before it.

## Errors and future integration

- Request construction raises Pydantic `ValidationError`.
- Invalid copied/unvalidated requests raise `FraudScoringError` before I/O.
- Stale/unknown freshness raises `GraphFreshnessError` before graph/model access.
- Failed graph queries or invalid returned features raise `GraphHistoryError`.
  Mixed-currency graph errors also fail; no artificial zero history is created.
- Existing safe model failures propagate as `ModelError`. Unexpected orchestration
  and guard errors become sanitized `FraudScoringError`. Guard-exit failure also
  prevents returning a score. Nothing logs raw dependency errors or input tokens.

The [Phase 4.2 workflow](scoring-api.md) now supplies SQL-backed freshness,
serialization, late-event handling, lazy dependencies and prediction persistence.
The read-only orchestrator remains independent of that workflow. Existing
transaction inputs still accept INR and blank device tokens; only scoring input
applies the stricter contract. Alerts remain future work.

Run all tests from `backend/` using the existing virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The new tests use fake inference/model dependencies and controlled in-memory
history. They require neither saved model binaries, datasets, nor AWS access.
