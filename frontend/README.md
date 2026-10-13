# RiskGraph frontend — Phase 6.2

React components are reusable screen pieces. The Overview and Transactions
pages share the Phase 6.1 layout, native Fetch client and local Vite proxy.
Graph Explorer and Investigations remain placeholders.

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
Recharts 3.10.1 is the only direct dependency added in this phase. The dashboard
route loads on demand so chart code is not in the initial transaction bundle.
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
will consume this parameter; no graph query or visualization is implemented here.
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
views. Real Neptune verification, Graph Explorer visualization, investigation
workflows and deployment remain outside Phase 6.2.
