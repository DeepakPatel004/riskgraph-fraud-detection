# Phase 3.1: IEEE-CIS preparation

For the next offline step, see [Phase 3.2 historical graph features](GRAPH_FEATURES.md).
The single model is documented in [Phase 3.3 classifier guide](CLASSIFIER.md).

This offline tool validates, profiles, maps and chronologically splits the IEEE-CIS
**training** CSVs. It neither imports backend services nor accesses RiskGraph's
database, graph adapters, AWS or a model. No dependencies are added: Python 3.11+
standard-library CSV, Decimal, SQLite, JSON and gzip are sufficient.

## Acquire and run

Sign in at the [official Kaggle competition data page](https://www.kaggle.com/competitions/ieee-fraud-detection/data),
accept its rules, download and extract `train_transaction.csv` and
`train_identity.csv` into `data/raw/`. Follow the competition terms; do not commit
or redistribute the files. The script never downloads data. Kaggle's separate
unlabelled test files and sample submission are deliberately unused.

From the repository root in PowerShell:

```powershell
.\backend\.venv\Scripts\python.exe -m ml_training.prepare_dataset
```

For the current local files, which are in `data/row/` (not `data/raw/`):

```powershell
.\backend\.venv\Scripts\python.exe -m ml_training.prepare_dataset --raw-dir data/row
```

All three directories are configurable:

```powershell
.\backend\.venv\Scripts\python.exe -m ml_training.prepare_dataset --raw-dir data/row --processed-dir data/processed/run2 --reports-dir data/reports/run2
```

Outputs are `data/processed/{train,validation,test}.jsonl.gz` and
`data/reports/profile.json`. Each gzip file contains one JSON object per line.
Decimal amounts are strings, so readers should use `Decimal`, not float. JSON
null means absent data. Gzip metadata is fixed for byte-reproducible output.
The existing `.gitignore` excludes all `data/` files, including temporary workspaces
and reports. Create `data/raw/` when downloading; no placeholder data is supplied.

The script refuses to overwrite existing output files. Rerun with fresh output
directories. Validation completes before writing dataset outputs. An I/O failure
during writing can leave incomplete files; discard that run and select new output
directories. A report alone is not an atomic completion marker for all outputs.
Allow disk space for the temporary SQLite workspace and compressed results.

## Architecture and validation

CSV input is streamed one row at a time. A uniquely named SQLite workspace in the
processed directory stores only selected identity attributes and prepared rows.
Primary keys reject duplicates in **either** input, including unmatched identities.
Indexed identity lookups implement a left join: every valid transaction remains,
even without identity data. This avoids loading the wide CSVs into RAM or multiplying
rows during a join. An index sorts rows by `(TransactionDT, numeric TransactionID)`.
The workspace is closed and removed even after validation fails. It has no connection
to `backend/riskgraph.db`.

Required transaction columns: `TransactionID`, `TransactionDT`, `TransactionAmt`,
`isFraud`; identity requires `TransactionID`. Optional missing fingerprint/attribute
columns are handled as missing. Headers must be unique and row widths must match.
IDs and relative seconds must be nonnegative signed-64-bit integers. Dates must fit
Python's datetime range. Labels must be exactly 0 or 1. Amounts must be finite,
positive, and fit the existing Numeric(18,2) contract after rounding. Zero or negative
amounts are rejected rather than silently dropped. Error messages name files, row
numbers and columns; they do not print raw IDs or fingerprint values.

The profile reports input row counts, matched/missing/unmatched identities, missing
counts/rates for **every source column**, original CSV storage types, prepared
semantic types, fraud distribution, rounding counts and per-partition counts,
fraud rates, missing devices and time ranges. Missingness is measured separately
on the two source tables, not an inferred joined-wide table. No raw examples or
identifier values enter the profile. Whole-dataset summaries are exploratory;
they must not be used to fit training transformations or tune model selection.

## Field mapping (version ieee-v1)

| Prepared field | Source and policy |
|---|---|
| `transaction_id` | Canonical decimal string of numeric `TransactionID`; leading zero variants are the same ID. |
| `amount` | `TransactionAmt` parsed with Decimal, rounded to 2 places with ROUND_HALF_EVEN for existing API/graph compatibility. |
| `TransactionAmt` | Original numerical Decimal value as a string, preserving sub-cent precision; not a second predictive feature. |
| `TransactionDT` | Original integer relative seconds, retained as ordering metadata. |
| `event_time` | **Synthetic** `2000-01-01T00:00:00Z + TransactionDT seconds`, serialized with UTC `Z`. Never a real transaction date. |
| `currency` | Explicit **USD prototype assumption**; the supplied fields do not verify it. The backend's INR default is not used. No conversion occurs. |
| `card_token` | SHA-256 of ordered named `card1, card2, card3, card4, card5, card6` values, including null positions and a version/kind prefix. |
| `device_token` | SHA-256 of ordered named identity `DeviceInfo, DeviceType, id_30, id_31, id_33` values with the same version/kind scheme. |
| `isFraud` | Binary target, retained exclusively for later training/evaluation. Never used in tokens or attributes. |
| `attributes` | Only transaction `ProductCD, M1, M2, M3, M4, M5, M6, M7, M8, M9`, retained as source strings/null. |

Fingerprint values are stripped of surrounding whitespace but otherwise remain
case-sensitive source strings: `1` and `1.0` are different fingerprint components.
Empty strings and case-insensitive `nan/null/none/na/n/a` become null everywhere.
Missing optional attributes remain null; no imputation is fitted. Absent identity
records and entirely missing device components produce a null device, not one
shared unknown node. An entirely missing card gets a transaction-specific token,
so unrelated missing cards do not connect. Partial fingerprints include explicit
missing positions. Raw fingerprint columns are absent from prepared outputs;
other identity columns, addresses, email domains, C/D/V columns are intentionally
not persisted in this initial narrow baseline.

These tokens are **approximate pseudonymous fingerprints**, not verified card or
device identities. Shared attributes can merge unrelated people/devices; changing
attributes can split one identity. A generic DeviceType/browser/OS combination
can create a very large false shared-device cluster. Partial cards can likewise
be weak identifiers. Hashing does not eliminate these semantic collisions or
prevent dictionary guessing. No salt, HMAC secret or raw card numbers are needed
for this public anonymized prototype; this is not a production tokenization system.

## Temporal splitting and leakage boundaries

Transactions are sorted by relative seconds, then **numeric** TransactionID for
deterministic ties. Choose cumulative group ends nearest 70% and 85% of all rows,
preferring the earlier group end when equally close. All rows with the same
TransactionDT remain together. This produces approximately 70% training, 15%
validation, 15% testing, with differences reported as actual counts/fractions.
Few or very large time groups can produce empty partitions; the report flags the
result rather than moving individual rows or resampling.

Every partition reports both class counts. Fewer than 20 examples of either class
produces a warning; 20 is only a diagnostic floor, not a statistical guarantee.
No SMOTE, oversampling or random shuffling is applied. Labels and future rows do
not construct tokens, predictive attributes or graph features. Partition sizes
use the full timeline, as required for splitting, but there are no fitted global
encoders, imputers, scalers or aggregates. IDs, relative seconds, synthetic dates,
tokens and original amount are metadata, not automatically model features.
The later training phase must explicitly choose predictors and fit learned
transformations on training only. Dataset source attribute causality and deployment
availability still need review. No historical graph features are generated here.

## Verify

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q
```

Tests generate tiny synthetic fixtures in an ignored, unique `data/test-preparation/`
directory, removed after each test. They do not read the real training data or
require Kaggle, credentials or network access.

## Learning notes

- **Class imbalance:** fraud is much rarer than ordinary transactions; high overall
  accuracy alone may hide a model that misses fraud.
- **Data leakage:** information unavailable at prediction time sneaks into training,
  making evaluation look better than real performance.
- **Train/validation/test split:** learn on the earliest partition, choose model
  settings on validation, and reserve the latest test partition for final evaluation.
- **Feature engineering:** turn available information into useful predictors;
  historical graph calculations and learned transformations are deferred.
- **Deterministic preprocessing:** the same inputs and code produce the same tokens,
  ordering, partitions and output bytes, making experiments repeatable.
