# RiskGraph — Step 1: Backend Foundation

An internship-focused graph-aware fraud detection and investigation platform. This milestone provides FastAPI, a SQLAlchemy schema, database health checking, and tests. **Fraud scoring and Amazon Neptune are not implemented yet.**

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

## Next milestone

Add Pydantic transaction schemas, `POST /api/v1/transactions`, `GET /api/v1/transactions`, and idempotency logic, before connecting to Neptune.

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

**Learning note:** Graph ingestion and model scoring are not implemented yet. `risk_score` and `risk_level` are null. The statuses `RECEIVED` and `PENDING` do not mean a background worker has already been started. These endpoints use demo-only tokenized identifiers and must not be deployed publicly without authentication and access restrictions.

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
