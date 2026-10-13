# RiskGraph frontend — Phase 6.3

React components are reusable screen pieces. The Overview and Transactions
pages share the Phase 6.1 layout, native Fetch client and local Vite proxy.
Graph Explorer now provides read-only Cytoscape.js networks. Investigations
remains a placeholder.

## Start and verify

Use Node 20.19+ (20.x) or 22.12+ and the existing backend virtual environment.
From the repository root, in separate PowerShell terminals:

```powershell
cd backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```powershell
cd frontend
npm ci
npm run dev
```

Open http://127.0.0.1:5173. From `frontend/`, run `npm test` and `npm run build`.
Tests use Node's built-in runner with small synthetic fixtures, not AWS or large
datasets. From `backend/`, run `.\.venv\Scripts\python.exe -m pytest -q`.
See the [project README](../README.md) for environment variables and proxy setup.
Phase 6.2 added Recharts 3.10.1; Phase 6.3 adds only Cytoscape.js 3.34.3.
Dashboard and Graph Explorer routes load on demand, keeping their visualization
libraries out of the initial transaction bundle.
Chart containers follow [Recharts' responsive sizing guidance](https://recharts.github.io/en-US/api/ResponsiveContainer/).

## Overview data sources

| GET endpoint | View and meaning |
| --- | --- |
| `/api/v1/dashboard/summary` | Total, scored, HIGH transactions and OPEN alerts |
| `/api/v1/dashboard/transaction-trends` | Daily transaction activity by UTC event date |
| `/api/v1/dashboard/risk-distribution` | LOW, HIGH, UNSCORED counts |
| `/api/v1/dashboard/alert-status` | OPEN, IN_REVIEW, CLOSED alert counts |

“Open investigations” maps to `open_alerts`, excluding IN_REVIEW. Counts reflect
saved SQL records. The UI never reruns inference, reclassifies scores, or queries
Neptune. HIGH is a saved review flag, not proof of fraud; LOW is not proof of
legitimacy. UNSCORED means no saved score. No invented growth or fraud percentages
are shown. Count axes begin at zero, use integer ticks, and animations are disabled.
Charts have tooltips; category legends also show exact counts. Recharts provides
keyboard chart navigation. Genuine zero datasets show empty states instead of
misleading charts. An empty database still has valid zero summary cards.

The default activity window is the backend's 30 UTC dates ending on the latest
stored event date, not today. Missing days come from the backend as real zero
counts. Dates/tooltips are explicitly UTC. Cards and risk/status charts summarize
the whole dataset, not just the activity window. Separate requests are not one
atomic dashboard snapshot and can disagree briefly during concurrent writes.

## Transactions and exact lookup

`GET /api/v1/transactions?limit=20&offset=0` returns `{items,total,limit,offset}`.
Next/Previous change the server offset in steps of 20. The backend orders by
descending event time, then descending ID. No all-record download or frontend
database filtering occurs. Concurrent inserts may shift page boundaries.

The table shows transaction ID, decimal amount, currency, UTC event time, saved
risk score/level, processing status and graph synchronization status. Amounts
retain their decimal precision. Scores display on the 0–1 scale to four decimals;
null displays a dash and UNSCORED, even if an inconsistent old row has a label.
Unsupported saved labels display UNKNOWN rather than an invented classification.
On narrow screens, only the table scrolls horizontally.

Click an ID or submit **Exact transaction-ID lookup** to call
`GET /api/v1/transactions/{transaction_id}`. This works across all stored records
but requires a complete, case-sensitive ID. It does not filter the current page
or support partial matching. A missing record shows a sanitized 404 message.
Details are shown inline with a close button and a link to
`/graph-explorer?transaction_id=...`, encoded using URLSearchParams. Phase 6.3
consumes this parameter and automatically requests the bounded network.
Only display fields are retained in transaction view state; card/device tokens
are omitted. No model internals or credentials are displayed.

## Requests, failures and limitations

`useApiResource` owns one AbortController per load. It cancels on navigation,
resource changes or refresh; `readResource` ignores late completions after abort.
The hook also hides previous data immediately when a page or selected ID changes.
Each dashboard section loads independently. Errors replace that section's data
with a sanitized message and manual retry; they never fabricate zero counts.
No automatic retries or polling run. Refresh clears old data while loading.
The shared health badge is the last `/health` result, not continuous monitoring.

Browser verification covers actual empty SQL responses and backend downtime.
Populated charts, pagination, exact lookup, details, and responsive layouts were
also verified through the same backend using a separate temporary database with
45 synthetic transactions and 15 synthetic alerts. These are UI fixtures, not
production data or model performance claims. The application database is untouched.

There is no authentication. Keep this a localhost-only demo. No creation,
scoring, alert updates, AWS calls, model loading or safety bypasses occur in these
views. Real Neptune verification, investigation workflows and deployment remain
outside this phase.

## Graph Explorer — Phase 6.3

Open `/graph-explorer` and enter an exact transaction ID (1–64 letters, digits,
underscores or hyphens). Choose a whole-number node limit from 1 to 100 (default
50), then **Load Graph**. The limit counts every entity, not only transactions.
The existing shared API client makes exactly one request:

```http
GET /api/v1/transactions/{transaction_id}/graph?limit=50
```

`/graph-explorer?transaction_id=TXN101` loads automatically. Optional `limit=...`
is also supported. A successful manual search pushes its ID/limit into the URL
without causing a duplicate fetch. Failed searches leave the last successful
URL unchanged. Browser Back/Forward loads that URL's network again. Requests
are aborted on navigation, new searches and unmount; late results are ignored.
No polling or additional graph pages are requested. Invalid/duplicate URL
parameters display validation errors without a graph request.

The response's `nodes` and `edges` become Cytoscape `{data: ...}` elements.
The mapper validates bounds, counts, node types, opaque IDs, root identity and
edge direction/endpoints, then whitelists safe fields. Unknown node/edge types,
missing roots, empty synchronized graphs and malformed responses become
controlled errors rather than fabricated successful networks. Amount/currency
remain absent if not supplied. Unexpected token properties are discarded;
card/device labels are derived only from the API's opaque hash IDs. Hashes
remain pseudonyms: the UI never reconstructs source tokens.

| Visual | Meaning |
| --- | --- |
| Blue rounded rectangle | Transaction |
| Teal circle | Card pseudonym |
| Purple diamond | Device pseudonym |
| Navy transaction with teal outline | Requested root transaction |
| Solid directed edge | Transaction USES_CARD |
| Dashed directed edge | Transaction USES_DEVICE |

Colors describe entity types, never fraud classifications. A relationship is
not proof of fraud. The network has no historical cutoff and must not become
model training/inference features.

Scroll/pinch to zoom, drag the background to pan, and click a node to inspect
it. Keyboard-accessible buttons also zoom, pan, fit the network and center the
selected node. The **Select a node** dropdown provides accessible selection of
every returned node. Details show type, safe label, opaque ID and permitted
transaction amount/currency. **View transaction** passes an encoded exact ID
to `/transactions?transaction_id=...`, which opens its existing detail lookup.
It does not filter the transaction table. Invalid transaction links never fetch
details. The built-in CoSE layout runs without animation; no editing/dragging
nodes, graph writes or deletions are enabled. Resize refits the graph; very dense
networks may require zoom or the dropdown to read individual nodes. Each mounted
viewer owns one Cytoscape instance and disconnects ResizeObserver/destroys it
on unmount or graph replacement. See [Cytoscape's API guide](https://js.cytoscape.org/)
for elements, styles and built-in layouts.

Counts and `truncated` come from the backend:

- `false`: no truncation reported for the currently visible network; this does
  not prove every SQL transaction reached Neptune.
- `null`: node limit reached; more nodes may exist. This is explicitly unknown,
  including a seed-only result at limit 1.
- `true`: display reported truncation if the backend ever supplies it.

404 means an unknown SQL transaction. 422 means invalid request values.
`GRAPH_SYNC_PENDING` (503) explains that synchronization is not ready; other
503 codes distinguish failed sync, unverified/mismatched population, missing
configuration, invalid networks and temporary dependency outages. Messages are
static/sanitized, with manual retry; no raw backend errors or payloads are logged.
The backend's population, SQL acknowledgement and synchronization checks remain
unchanged. Real Neptune is unverified, so normal successful graph access is not
claimed. There is no fallback graph in the real application.

## Isolated graph browser fixture

With Vite running, open:

```text
http://127.0.0.1:5173/tests/fixtures/graph-explorer.html?transaction_id=TEST_101
```

This clearly labeled frontend-only test page injects synthetic responses into
the real page/viewer. It makes no API/AWS calls and changes no backend settings.
Its request counter verifies single deep-link loads and Back/Forward behavior.
Use `TEST_100_NODES` with limit 100 for the maximum-size fixture; `PENDING`,
`UNKNOWN` and `UNAVAILABLE` exercise errors; `TEST_SLOW` exercises cancellation.
Transaction-link navigation on this fixture page shows a test-only handoff,
not real transaction details. The normal application uses the existing backend
for those details. Fixtures live only under `tests/fixtures/`, are not imported
by production code and are not included by `npm run build`.

Verification covers small/100-node networks, root highlighting, canvas and
dropdown selection, zoom/pan/fit, mobile/tablet containment, URL synchronization,
Back/Forward, cancellation, errors and null truncation. Real application checks
use the existing empty database for 404 and validation behavior. These fixtures
are UI checks, not fraud evidence, performance claims or Neptune verification.
