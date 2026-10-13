# RiskGraph — Fraud Detection & Investigation Platform

Offline ML data preparation: see [Phase 3.1 IEEE-CIS guide](ml_training/README.md)
for local input paths, field mapping, synthetic UTC dates and chronological splits.

An internship-focused modular monolith with FastAPI transaction, scoring, alert,
graph exploration and dashboard APIs, a Neptune adapter, and one pretrained
XGBoost classifier. Real Neptune deployment and verification remain pending.
The React frontend provides a SQL-backed Overview dashboard, paginated
transactions and a read-only Graph Explorer. Investigations remains a placeholder.
Normal graph access still requires real Neptune population verification.

## Frontend — Phase 6.3

Use Node.js 20.19+ (20.x) or 22.12+; this phase was verified with Node 24.16.0
and npm 11.13.0. From the repository root, in one PowerShell terminal:

```powershell
cd backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Use the existing backend environment and database. For a fresh backend setup,
follow the installation instructions below. In a second terminal:

```powershell
cd frontend
npm ci
npm run dev
```

Open http://127.0.0.1:5173. Routes are `/`, `/transactions`, `/graph-explorer`,
and `/investigations`. React Router switches pages inside a shared layout.
Reusable components retain the navy sidebar, teal accents, connection badge,
and loading/error/empty states. Overview displays four real metric cards and
three Recharts visualizations. Transactions displays 20 records per page,
saved risk classifications, UTC times, exact-ID lookup and record details.
Graph Explorer uses Cytoscape.js with bounded networks, safe node details,
zoom/pan/select controls and transaction-ID deep links. It never substitutes
synthetic data for unavailable graph responses.
See [the frontend guide](frontend/README.md) for contracts and limitations.

Optional configuration: copy `frontend/.env.example` to `frontend/.env.local`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `VITE_API_BASE_URL` | `/backend` | Public API base URL used by the browser |
| `VITE_API_TIMEOUT_MS` | `8000` | Request timeout, integer 1–60000 milliseconds |
| `DEV_API_TARGET` | `http://127.0.0.1:8000` | Server-side local Vite proxy target |

The Vite development proxy forwards `/backend/health` to FastAPI `/health`,
and likewise forwards `/backend/api/v1/...`. This avoids changing backend CORS.
Restart Vite after changing environment settings. `VITE_` variables are public
in the browser bundle: never put credentials or secrets in them. An absolute
API URL requires that backend to explicitly allow the frontend origin.

`src/api/client.js` centralizes native Fetch, timeout/cancellation, JSON,
encoded query parameters and sanitized errors. `src/api/endpoints.js` provides
dashboard, transaction, alert and graph helpers matching the backend contracts.
It preserves decimal strings, pagination envelopes and nullable graph metadata.
`ApiError` exposes `status`, `code`, `retryable`, `retryAfter` and validation
messages, supporting both structured errors and legacy `detail` responses.
The UI never displays raw backend error bodies. Write helpers are available
for future explicit actions but are not called by these read-only pages.

The shared layout checks `/health` once; its refresh button manually checks again.
The connection badge reflects the last health check, not continuous monitoring.
Overview loads four dashboard endpoints once on entry, while Transactions loads
one bounded page. Both provide manual refresh without polling. Navigation aborts
pending page requests and ignores late responses. Connected means FastAPI and
SQL are reachable, not that Neptune or the model is ready. A valid graph deep
link or explicit Load Graph action requests one bounded network. No scoring
requests run automatically. There is no authentication;
keep both servers bound to localhost and use pseudonymized demonstration data.
The existing graph population and scoring safety checks remain unchanged.

Verification commands from `frontend/`:

```powershell
npm test
npm run build
npm run preview
```

Tests use Node's built-in test runner and fake Fetch responses, with no added
test framework or AWS access. Preview opens at http://127.0.0.1:4173 and includes
the same **local-only** proxy. A production static host does not run Vite's
proxy: it needs an explicitly configured API reverse proxy and SPA route
fallback, plus authentication/access controls before public deployment.

Troubleshooting:

- **Disconnected:** check http://127.0.0.1:8000/health, the backend terminal,
  and `DEV_API_TARGET`, then use the refresh button. The frontend remains usable
  while the backend is unavailable.
- **Port already in use:** stop your own previous development server or set a
  different port with `npm run dev -- --port 5174`. Vite fails clearly rather
  than silently moving to another port.
- **Configuration error or timeout:** check the URL and timeout above; restart
  Vite after edits. Do not paste credentials into URLs.
- **Direct URL fails on a static host:** configure SPA fallback to `index.html`.
  Local Vite development and preview support direct route navigation already.

React components are reusable pieces of the screen. React Router chooses which
page to show for a URL. Vite runs the local development server and builds the
browser files. API integration is the shared code that asks FastAPI for data
and handles its replies and failures.

## Requirements
- Python 3.11+
- Windows PowerShell, macOS, or Linux

## Run locally (SQLite, no AWS required)

From the repository root:

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m scripts.init_db
python -m uvicorn app.main:app --reload
```

For Linux/macOS use `source .venv/bin/activate` instead of the PowerShell activation line.

Open http://127.0.0.1:8000/docs and test `GET /health`.

## Run tests

From `backend/` with the virtual environment active:

```powershell
python -m pytest -v
```

The tests use temporary in-memory SQLite databases and don't connect to AWS.

## Switch to PostgreSQL later

Create `backend/.env` with:

```env
DATABASE_URL=postgresql+psycopg://riskgraph:yourpassword@localhost:5432/riskgraph
APP_NAME=RiskGraph API
```

Ensure PostgreSQL is running and the `riskgraph` database and user exist; then rerun `python -m scripts.init_db`. For production, replace `create_all` with database migrations (Alembic). Never commit `.env` to Git.

## Design notes

- `api/` holds HTTP endpoints; `models/` holds SQLAlchemy entities; `core/` holds configuration and database wiring.
- The `/health` endpoint checks SQL connectivity, not Neptune or model availability. It is a *basic database readiness check*, not a complete production health strategy.
- Status fields are strings in this milestone. Later steps will enforce transitions in service logic and validate input with Pydantic schemas.
- Database schema and SQL initialization are for local learning; future migrations will handle schema evolution.
- This is a prototype using only test, simulated, or pseudonymized data; do not store real payment card numbers.

## Step 2 — Transaction APIs

No schema migration is necessary when upgrading from Step 1. The transaction and idempotency tables already exist.

Start from `backend/`:

```powershell
python -m pip install -r requirements.txt
python -m scripts.init_db
python -m uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs and try:

- `POST /api/v1/transactions` to create a transaction.
- `GET /api/v1/transactions` to list transactions (`limit=20`, `offset=0`).
- `GET /api/v1/transactions/{transaction_id}` to fetch a transaction.

Example payload:

```json
{
  "transaction_id": "TXN1045",
  "amount": "15000.50",
  "currency": "INR",
  "card_token": "TOKEN_CARD_22",
  "device_token": "TOKEN_DEVICE_11",
  "event_time": "2026-10-10T10:30:00Z"
}
```

You may optionally send header `Idempotency-Key: retry-123`. First creation returns HTTP 201; the same request with the same key returns HTTP 200 and the same record. A different payload with the same key returns HTTP 409. Duplicate transaction IDs also return 409. Validation errors return 422; missing transaction IDs return 404.

Run tests:

```powershell
python -m pytest -v
```

**Learning note:** Legacy transaction creation leaves `risk_score` and `risk_level`
null; scoring uses its separate endpoint and readiness checks. The statuses
`RECEIVED` and `PENDING` do not mean a background worker has already been started.
These endpoints use demo-only tokenized identifiers and must not be deployed
publicly without authentication and access restrictions.

## UTC timestamps

Transaction timestamps must include an offset (such as `+05:30`) or `Z` for UTC.
The API converts accepted timestamps to UTC before persistence and idempotency hashing.
For example, `2026-10-10T10:30:00+05:30` is returned as `2026-10-10T05:00:00Z`.

All six model timestamp columns use the reusable `UTCDateTime` SQLAlchemy type.
SQLite stores UTC clock values in its existing `DATETIME` format without an offset;
the type restores UTC timezone information when reading. PostgreSQL uses
`TIMESTAMP WITH TIME ZONE`, and returned values are converted to UTC regardless
of the database session timezone. System defaults use `datetime.now(timezone.utc)`.

No schema migration or data rewrite is required. Existing naive timestamp values
are assumed to represent UTC and are read with UTC attached. An offset discarded
by older code cannot be recovered automatically. Previously saved idempotency
hashes are not rewritten; an old hash based on a non-UTC timestamp representation
may conflict on retry after normalization. The local database was inspected and
contained no rows at the time of this change.

## Local graph abstraction

`app/graph/repository.py` defines the synchronous `GraphRepository` Protocol.
`InMemoryGraphRepository` is a local implementation for learning and tests; it
is not wired into the transaction API and does not connect to AWS. A future
Neptune adapter can implement the same three methods without changing callers.

Pass a `TransactionNode` containing the transaction ID, Decimal amount, currency,
aware event time, pseudonymous card token, and optional device token. Never pass
raw card numbers. The graph contains directed `USES_CARD` and `USES_DEVICE`
relationships from transactions to token nodes. Repeated upserts replace the
transaction by ID and keep edges unique; changed tokens replace old edges.

`get_transaction_network(id, limit=50)` follows edges in both directions using
breadth-first traversal, including indirect connections. The positive integer
limit counts all returned nodes, including the root. Results are deterministic,
contain only edges whose endpoints are returned, and are empty for unknown IDs.
This investigation view has no historical time filter and must not be used as
input to historical feature calculation.

`get_historical_features(card_token, device_token, event_time)` uses elapsed UTC
windows `[event_time - window, event_time)`: lower bounds are inclusive; exact-time
and future events are excluded. It returns card/device transaction counts for
24 hours, distinct card/device associations for 7 days, and the card's 7-day
average amount using Decimal without rounding to cents. Absent devices are not
counted as unique devices. Missing histories return zero; an absent device gives
zero device features. Mixed currencies in a card's 7-day history raise ValueError
because no currency conversion or currency parameter is available. Graph input
contains no fraud labels or model predictions.

The in-memory adapter scans local data and loses it on restart. It is intended
for small datasets and serialized use, not concurrent writers or production
storage. No database schema changes or additional dependencies are required.

## Neptune adapter (offline milestone)

`NeptuneGraphRepository` now implements the same contract using a small HTTPS
client. It is not activated by the API. No AWS resources, credentials, or network
access are needed for the tests. See [the Neptune adapter guide](docs/neptune-adapter.md)
for request encoding, explicit construction, optional IAM dependency, and the
integration checks that still require a real Neptune Database.

## Transactional graph outbox

Transaction creation now commits one `GraphSyncJob` with the transaction and its
optional idempotency record. Graph writes run separately through an explicitly
started worker. Existing API field names and endpoints are preserved.

For an existing database, stop the API/workers, make a database backup, and run
`python -m scripts.migrate_graph_sync_leases` from `backend/`. Fresh installations
using `python -m scripts.init_db` already include the new nullable lease column.

See [the graph sync guide](docs/graph-sync.md) for worker commands, retry policy,
lease recovery, manual retries, and important in-memory/production limitations.
