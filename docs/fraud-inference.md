# Phase 3.4: read-only model inference

See the [Phase 4 integration contract](phase-4-inference-contract.md) for the
verified handoff requirements and remaining integration decisions.

`app.services.fraud_inference_service` turns validated inputs into a score, risk
level and model version. It does not query SQL/Neptune, write records, create alerts
or change graph sync jobs. No public endpoint or SQL schema is changed. The Phase 4
workflow must fetch historical features and decide how to persist a score later.

The reusable predictor now lives in `backend/app/ml/fraud_predictor.py`, so normal
backend commands do not depend on the repository root being on Python's import
path. `ml_training/fraud_predictor.py` preserves the old offline import interface.
This is a runtime code move, not a model change. The saved preprocessing contains
standard sklearn objects, not custom classes from the moved module.

## Feature schema and mapping

The exact 17 raw feature names and order remain:

```text
amount
card_txn_count_24h
device_txn_count_24h
card_unique_devices_7d
device_unique_cards_7d
card_avg_amount_7d
device_missing
ProductCD
M1
M2
M3
M4
M5
M6
M7
M8
M9
```

| Internal inference input | Model mapping |
|---|---|
| `InferenceTransaction.amount` | `amount`, converted from Decimal to ML float using the original predictor. |
| `InferenceTransaction.attributes.ProductCD` | Nested training `attributes.ProductCD`. |
| `InferenceTransaction.attributes.M1`–`M9` | Corresponding nested training M fields. |
| Five `GraphFeatures` fields | Same five top-level historical feature names, unchanged. |
| `InferenceTransaction.device_missing` | Required strict boolean converted to integer 0/1. |
| `InferenceTransaction.currency` | Required USD compatibility guard; never a model feature. |

IDs, tokens, timestamps and labels are not accepted by the inference schema.
Unexpected transaction/attribute fields are rejected. GraphFeatures must be the
existing dataclass, and its values are revalidated because Python dataclasses do
not enforce annotated types. Counts must be nonnegative integers (not booleans,
strings or fractional values); the average must be a finite nonnegative Decimal.
It retains sub-cent precision. Amount must fit the existing positive Numeric(18,2)
contract. Other unsupported numeric ranges are rejected by the original predictor.

Missing ProductCD/M1–M9 remain `None`. All ten fields are optional; omitting the
attributes object means all ten are unavailable. The service does not guess a
product, turn missing matches into `F`, or derive these fields from tokens. Empty
strings and explicit `__MISSING__` source values are rejected: callers must use
null. Nonempty unseen strings are accepted; the saved encoder handles them.

The **saved** imputer maps null categories to its trained `__MISSING__` marker,
then the saved encoder transforms them. If that marker/category was not observed
in training for a field, the existing unknown-category policy yields zeros for
that field's encoding. No category is fitted or added during inference. The
existing real bundle expands 17 raw fields into 40 encoded inputs.

The service rejects contradictory histories: absent devices cannot have device
history, a positive 24-hour device count requires seven-day card associations,
and card activity requires a positive seven-day average because historical amounts
are positive. Zero history is valid, but unavailable history must not be disguised
as zero history. The caller is responsible for supplying authentic historical data.

## Load once and predict

Install the existing optional `backend/requirements-ml.txt` environment if needed;
this phase adds no dependencies. From `backend/`, with the virtual environment
active, a sample call is:

```python
from decimal import Decimal
from app.graph.models import GraphFeatures
from app.schemas.fraud_inference import InferenceTransaction, TransactionModelAttributes
from app.services.fraud_inference_service import get_fraud_inference_service

# Once during application startup; retain this service for subsequent requests.
# Defaults to <repository>/artifacts/fraud_classifier, independent of cwd.
service = get_fraud_inference_service()

# Synthetic sample with explicitly supplied USD and genuinely absent categories.
transaction = InferenceTransaction(
    amount=Decimal("100.00"), currency="USD", device_missing=True,
    attributes=TransactionModelAttributes(),
)
# This sample explicitly represents an empty history, not a failed graph lookup.
result = service.predict(transaction, GraphFeatures())
print(result.model_dump())
```

The unchanged real bundle produced score `0.24356554448604584` and risk level `LOW`
for this sample. Repeated predictions were identical. This is a smoke check, not
a new evaluation or evidence that the sample is legitimate.

An explicit bundle can be supplied to `get_fraud_inference_service(path)`. Paths are
canonicalized; a startup lock serializes initial loading, and a single-entry service
cache retains the loaded predictor. Predictions themselves neither load files nor
invalidate either cache. Use one fixed path per process; restart to change the model.
Each worker process has its own model. Importing API modules does not load artifacts,
and this phase does not wire the service into FastAPI startup or endpoints.

The loader verifies bundle hashes, raw/transformed feature order, 17-input
preprocessor dimensions, classifier dimensions/classes, binary logistic objective,
saved best iteration, and recorded dependency versions before inference. Use the
pinned ML dependencies. Only locally trusted joblib artifacts should be loaded;
checksums detect corruption rather than authenticate a bundle's author.

Missing/corrupt/incompatible artifacts raise `ModelError`. Invalid input models
raise validation errors; the service sanitizes failures from revalidation or
prediction. There is no fabricated fallback score, and no transaction payload or
token is logged. Callers should avoid logging full input/validation payloads.

The saved threshold `0.7617719769477844` is applied unchanged: score greater than
or equal to it is `HIGH`; otherwise `LOW`. No new threshold or medium level is
invented. The response has only `risk_score`, `risk_level`, and `model_version`.
The version is `xgb-v1-` plus the SHA-256 of the verified canonical bundle manifest,
so it identifies weights, preprocessing, schema and configuration together. The
version is derived in memory; no file in the existing bundle is modified.

## Training-serving compatibility findings

- The public `TransactionCreate` schema currently has no ProductCD/M1–M9 fields.
  The SQL transaction service does not populate them. `features_json` exists in
  the SQL model but is not populated from public requests. Phase 4 needs an explicit
  source/policy for these dataset-specific attributes; absent values remain null.
- The API defaults to **INR**, while this model was trained using an assumed
  **USD** currency. Passing INR is rejected. This service does not relabel INR as
  USD or perform FX conversion. Resolve currency support before API integration.
- The API permits an empty device token, while graph TransactionNode rejects it.
  Future workflow validation must resolve that mismatch. For a valid transaction,
  derive `device_missing` with `device_token is None`, not general truthiness; the
  inference service deliberately accepts no token itself.
- GraphFeatures contains no cutoff or currency provenance. The future caller must
  obtain it from `[event_time-window, event_time)` for the correct card/device,
  excluding every exact-time/future record. This service can check value consistency,
  but cannot prove temporal provenance from five aggregate values alone.
- LOW/HIGH are descriptive flags at the saved threshold, not verified fraud labels.
  Existing modest test performance, approximate fingerprint identities, missing
  categories and uncalibrated scores remain limitations. No performance claim was
  changed and no additional model was trained.

## Tests and simple concepts

Run the complete suite from `backend/`:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

New service tests use synthetic artifact stubs and mocked model/preprocessing
loading, with no fitting and no dependency on the real bundle or datasets. They
check startup cache/concurrency, compatibility, mapping, types, thresholds, score
bounds, failures, and forbidden SQL/graph side effects. Existing Phase 3.3 tests
retain their original small synthetic classifier fixture; the real model is never
trained by the test suite. A separate local smoke test read the existing bundle.

**Model inference** means using already learned trees to score a new input, without
learning again. A **feature schema** names the inputs and fixes their meaning and
order so training and serving agree. **Model caching** keeps the loaded trees and
preprocessing in memory so subsequent predictions reuse them instead of reading
the files each time. Reusing saved preprocessing also preserves training medians,
category mappings and encoded-column order.
