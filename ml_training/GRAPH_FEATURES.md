# Phase 3.2: historical graph features

This offline step enriches the Phase 3.1 `train.jsonl.gz`, `validation.jsonl.gz`
and `test.jsonl.gz` files. It does not create a full Python graph, call Neptune,
change the API or application database, or train a model. It adds no dependencies;
the existing backend environment supplies Pydantic for the existing graph models.

From the repository root:

```powershell
.\backend\.venv\Scripts\python.exe -m ml_training.build_graph_features
```

Or choose paths:

```powershell
.\backend\.venv\Scripts\python.exe -m ml_training.build_graph_features --input-dir data/processed --output-dir data/graph_features/run2
```

The default output directory is `data/graph_features/`, with the three enriched
gzip JSON Lines files and `report.json`. **Choose a new output directory for each
run**: any existing destination, even an empty one, is refused. Inputs must be
the original Phase 3.1 schema; already enriched or unexpected raw fields are
rejected. All data and reports remain ignored by Git through the existing `data/`
rule. No original prepared file is overwritten.

The script stages files in a unique sibling directory. After all files close and
validation succeeds, it renames that directory to the destination. A normal failure
removes only that run's private staging directory. A killed process may leave an
ignored `.graph-features-*` directory; it is not a successful output. No automatic
resume or replacement of an existing run is attempted. Do not run multiple writers
for the same destination. Temporary disk-backed ID checks use their own SQLite
file, never `backend/riskgraph.db`.

## Added fields

| Field | Definition using transactions strictly before T |
|---|---|
| `card_txn_count_24h` | Count with this card in `[T - 24h, T)`. |
| `device_txn_count_24h` | Count with this device in `[T - 24h, T)`; zero if absent. |
| `card_unique_devices_7d` | Distinct non-null devices used with this card in `[T - 7d, T)`. |
| `device_unique_cards_7d` | Distinct cards used with this device in `[T - 7d, T)`; zero if absent. |
| `card_avg_amount_7d` | Decimal average of this card's amounts in `[T - 7d, T)`; zero if empty. |
| `device_missing` | Integer 1 for an absent device token, otherwise 0. |

All original fields are preserved, including transaction ID, `attributes`, and
`isFraud`. The five features and missing-device flag are top-level fields.
Counts/flag are JSON integers; the average is a Decimal string with no additional
rounding to cents. It uses the existing prepared `amount`, not original sub-cent
`TransactionAmt`. Later training must select predictive columns explicitly;
`isFraud` is exclusively a target, not part of feature calculation or state.

## Incremental algorithm and historical correctness

`HistoricalFeatureBuilder` uses two chronological queues, one for 24 hours and one
for seven days, shared across cards/devices. Dictionaries hold per-token 24-hour
counts and per-card seven-day sums, counts, currencies and device frequencies.
Per-device frequency maps track cards. When time advances, expired events decrement
these aggregates. Zero-frequency entries and unused tokens are removed. A repeated
card/device pair counts as one distinct relationship until its final active event
expires. Missing devices never create an unknown-device node or pair.

Each event is added to and removed from each queue once. Queries use dictionary
lookups and distinct-key counts, not history scans. Feature calculation is
approximately O(N) for a fixed-size currency set. Working memory depends on the
active seven-day history plus the largest group of equal-time rows, not all 590K
transactions. Separate SQLite primary keys reject duplicate IDs across the entire
dataset, including those no longer retained in memory.

The input must already increase by `(TransactionDT, numeric transaction_id)`.
Dates must be timezone-aware and agree with Phase 3.1's **synthetic** UTC reference.
Equal TransactionDT rows form a complete group. The builder expires events older
than the lower boundary, calculates **every** feature vector for the group, then
adds the group. It never includes the current event, another equal-time event or
a future event. It rejects time regressions and separately submitted repeated
timestamp groups. The pipeline rejects a timestamp group spanning two partitions
rather than silently changing membership.

Historical state carries from training into validation and then testing. This is
valid for an online detector: when a later transaction arrives, the system has
already observed earlier transactions and their card/device associations. It does
not need their fraud labels to count them. Using earlier validation/test events as
history for still-later events simulates that stream; using their labels, future
events or model predictions would be leakage. No learned imputer, encoder, scaler
or model is fitted here.

## Currency and precision

The real prepared data explicitly uses the Phase 3.1 USD prototype assumption;
there is no currency conversion and USD remains unverified source metadata.
The builder preserves the graph adapter's exact rule: reject an average if the
card's **historical seven-day** events contain more than one currency. A current
transaction with a different currency does not itself invalidate the historical
average; once added, it can make a later query invalid. Unrelated cards and expired
currencies do not invalidate a query.

Running sums use Decimal, so cents are exact within the existing Numeric(18,2)
input limits and dataset size. Division follows the existing Decimal context
(normally 28 significant digits), just like `InMemoryGraphRepository`. A later
training loader may convert features to floating point deliberately; this step
does not do so. Low-level builder input is append-only with unique IDs, unlike the
reference repository's correcting upsert. Duplicate pipeline IDs are rejected
instead of replacing old transactions, which would invalidate previous features.

## Report and verification

`report.json` includes row counts per split, missing-device percentage, feature
min/max/mean and fixed disjoint histogram buckets (`0`, `(0,1]`, `(1,5]`, `(5,10]`,
`(10,50]`, `(50,100]`, `>100`), and percentages with no prior seven-day card/device
history. The device percentage over **all rows** includes missing devices; a
separate percentage covers only present devices. No prior card history means no
card events in the seven-day window, not merely no 24-hour activity. Original
labels and raw token values are not included in the report.

The report also records successful schema, UTC, unique-ID, ordering and split
integrity checks and SHA-256 checksums for each enriched file. These are validations
of this run, not proof of source identity quality. The histogram is a bounded-size
distribution summary; exact percentiles are not computed. Full-dataset summaries
are for inspection, not learned preprocessing or model tuning.

Duration measures loading, feature calculation and compressed file writing up to
report construction; final checksumming/publication is excluded. Peak memory is
the OS-reported **whole-process** peak working set on Windows or RSS on supported
Unix systems, including interpreter overhead; it is not an exact feature-state
allocation count. An unavailable measurement is reported as null. For comparable
measurements, run the CLI in a fresh process. Dataset output bytes are deterministic;
report duration and peak memory naturally vary.

Run tests from `backend/`:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Small synthetic fixtures compare all five features against the existing in-memory
repository, preloaded with exact-time and future events to exercise cutoff behavior.
Tests cover precise boundaries, missing devices, duplicate pair expiry, mixed
currencies, UTC offsets, sub-cent averages, cross-split history, changed labels,
invalid input, output safety and deterministic dataset bytes. Ordinary tests need
no real dataset, credentials or network.

The existing approximate-token limitation remains: shared anonymized attributes
can merge unrelated cards/devices. Historical correctness prevents time leakage;
it does not turn these fingerprints into verified identities. This phase stops
after feature generation; XGBoost training remains a later task.
