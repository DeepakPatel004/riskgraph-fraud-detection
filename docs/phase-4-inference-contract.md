# Phase 3.5: verified inference contract for Phase 4

Phase 4.1 now supplies a separate scoring input and read-only orchestrator; see
[fraud scoring](fraud-scoring.md). Public scoring endpoints and a persistent
freshness checker remain future work. The inference contract below is unchanged.

Inference means using the existing trained model to score one transaction.
This contract describes future integration; no scoring endpoint or startup wiring
exists yet. The service only transforms inputs and returns a prediction. It does
not fetch graph history, persist records, create alerts or modify sync jobs.

## Inputs and feature order

Phase 4 must construct `InferenceTransaction` and supply `GraphFeatures` to
`FraudInferenceService.predict(transaction, features)`.

| Input | Meaning and validation |
|---|---|
| `amount` | Positive finite Decimal, at most 18 digits and two decimal places. |
| `currency` | Explicit `USD` only. Compatibility guard, not a model feature. No FX conversion. |
| `attributes.ProductCD` | Product category string or null. |
| `attributes.M1` through `attributes.M9` | Each a string or null, with the original dataset field meaning. |
| `device_missing` | Required strict boolean; becomes integer 0/1 in the model. |
| `card_txn_count_24h` | Number of prior transactions for this card in 24 hours. |
| `device_txn_count_24h` | Number of prior transactions for this device in 24 hours. |
| `card_unique_devices_7d` | Distinct nonmissing devices used with this card in seven days. |
| `device_unique_cards_7d` | Distinct cards used with this device in seven days. |
| `card_avg_amount_7d` | Mean prior card transaction amount in seven days, in USD. |

Counts must be nonnegative integers, excluding booleans and numeric strings.
The average must be a finite nonnegative Decimal; retain sub-cent precision.
The service rejects inconsistent aggregates, such as device activity for an absent
device or card activity with a zero historical average.

The **17 model features**, in saved order, are:

```text
amount, card_txn_count_24h, device_txn_count_24h,
card_unique_devices_7d, device_unique_cards_7d, card_avg_amount_7d,
device_missing, ProductCD, M1, M2, M3, M4, M5, M6, M7, M8, M9
```

Omitted categorical attributes remain null, including when the entire attributes
object is omitted. Do not guess values, substitute `F`, or derive categories from
tokens. Blank strings and the internal `__MISSING__` marker are rejected. Nonempty
unseen strings are accepted and use the saved encoder's all-zero unknown-category
encoding. Nulls use its saved missing-value handling. Reuse fitted preprocessing:
refitting would change category columns and training medians. The feature schema
is the agreed list, meaning and order of inputs; this bundle produces 40 encoded
columns. IDs, tokens, timestamps and labels are never model features.

## Future FastAPI workflow and historical cutoff

1. At process startup, call `get_fraud_inference_service()` and retain the service
   through application state/dependency injection. Pass an explicit artifact path
   if required. There is currently no environment setting for that path.
2. Obtain validated amount, explicit USD currency and categorical attributes from
   an agreed source. Validate device tokens consistently with the graph contract;
   derive `device_missing` using `device_token is None` after validation.
3. Call `GraphRepository.get_historical_features(card_token, device_token,
   event_time)` with the correct pseudonymous tokens and timezone-aware UTC cutoff.
4. Construct the inference input and call `service.predict(input, features)`.
   Phase 4 owns any later persistence, idempotency and alert decisions.

History uses elapsed windows **[event_time - window, event_time)**: include the
lower boundary; exclude all exact-time and future transactions, including the
current transaction. Counts use 24 hours; distinct associations and the average
use seven days. Never use labels, predictions or the investigation network as
history. A genuinely empty history returns zeros; an absent device has zero device
features. A failed graph lookup must never be replaced with zeros.

The graph repository rejects mixed currencies in seven-day card history, but
aggregates carry no currency or cutoff provenance. Phase 4 must also ensure that
single-currency history is USD and that the cutoff is correct. Outbox graph sync
is asynchronous: SQL may contain earlier events not yet in Neptune. Phase 4 must
define a history-readiness policy before treating scores as complete. A correct
cutoff alone does not prove complete history.

## Output, errors and artifact configuration

The response contains exactly:

```json
{"risk_score": 0.24356554448604584, "risk_level": "LOW", "model_version": "xgb-v1-3393fbaabb6c045a04db6d08a58d382934c5364333186cc7c33a033efd3104b7"}
```

Scores are finite numbers in [0,1]. `HIGH` means score **>= 0.7617719769477844**;
otherwise the level is `LOW`. These are model flags, not confirmed fraud labels
or calibrated guarantees. Internal model/preprocessing objects are not returned.

Input construction raises Pydantic `ValidationError` for unsupported currency,
malformed types and extra fields. The service raises `ModelError` for invalid
copied/constructed inputs, inconsistent history, loading failures or prediction
failures. Graph lookups can fail separately, including `ValueError` for mixed
currency history. Phase 4 must define HTTP mappings and readiness behavior;
there is currently no scoring HTTP contract. Never return a fabricated score or
log full input/error payloads containing tokens.

The default bundle is `<repository>/artifacts/fraud_classifier/`, containing
`model.ubj`, `preprocessing.joblib`, `schema.json`, `configuration.json`,
`metrics.json` and `manifest.json`. Use trusted local artifacts and the pinned
`backend/requirements-ml.txt` environment. Hashes detect corruption, not malicious
replacement. The loader checks hashes, feature order/dimensions, objective,
classes, best iteration and recorded dependency versions. The model version
identifies the complete canonical manifest, including threshold configuration.

Model caching keeps one fixed bundle in memory per Python process. Repeated
predictions reuse it; each worker loads its own copy. Restart to change artifacts;
there is no hot reload. Missing/incompatible artifacts must fail readiness rather
than produce a fallback score.

## Verification and remaining decisions

Phase 3.5 loaded the existing bundle without training or dataset processing.
All six artifact hashes remained unchanged. The saved configuration agrees with
the 17/40 input dimensions, binary logistic objective, best iteration 293 and
300 stored boosting rounds. Saved validation/test thresholds, row counts,
precision, recall and F1 agree with their confusion matrices. Saved average
precision/ROC-AUC were checked as stored values, not recomputed on datasets.

Three fixed synthetic smoke inputs produced:

| Input | Score | Level |
|---|---:|---|
| USD 100, absent device/categories, empty history | 0.24356554448604584 | LOW |
| USD 25, ProductCD W/M1 T; card/device counts 2/1, unique devices/cards 1/1, average 20 | 0.3889167308807373 | LOW |
| USD 250, ProductCD C/M4 M2/M9 F; counts 10/7, unique devices/cards 3/4, average 100 | 0.9653785824775696 | HIGH |

The last two inputs had present devices; other categorical values were null.
Repeated predictions were identical and threshold classification was consistent.
INR, negative amounts and boolean categorical values were rejected. These checks
verify execution, not predictive performance. No new performance evaluation ran.

Before Phase 4 integration, resolve the API's INR default, its lack of ProductCD/
M1-M9 fields, and acceptance of empty device tokens that graph nodes reject.
Choose a validated category source (or honest nulls), ensure new semantic inputs
participate in idempotency if the API is extended, and define graph readiness and
prediction provenance persistence. Current SQL/API schemas remain unchanged.
