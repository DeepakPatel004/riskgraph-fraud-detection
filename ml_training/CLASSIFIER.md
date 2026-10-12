# Phase 3.3: one graph-aware XGBoost classifier

Backend readiness and strict serving inputs are documented in the
[Phase 3.4 inference guide](../docs/fraud-inference.md). The reusable predictor
implementation now lives in `backend/app/ml/`; its original offline import remains
available through `ml_training/fraud_predictor.py`. Existing artifacts are unchanged.

This is a supporting fraud scorer for the backend project: one binary classifier,
one configuration, one validation-selected threshold, and one final test evaluation.
There is no baseline comparison, hyperparameter search or separate model variant.
FastAPI, Neptune, the outbox and original datasets are unchanged.

## Install and reproduce

Run from the repository root using Python 3.11+ with compatible wheels. The pinned
versions were installed and tested with CPython 3.14.5 on Windows x64:

```powershell
.\backend\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r backend/requirements-ml.txt
.\backend\.venv\Scripts\python.exe -m ml_training.train_classifier
```

Inputs default to the existing `data/graph_features/` gzip JSON Lines partitions.
The Phase 3.2 report supplies expected row counts and checksums, which are verified
as each partition loads. A missing, changed or empty partition fails clearly.
Training and evaluation require both target classes in each partition.

The default bundle destination is `artifacts/fraud_classifier/`. Existing output
directories are refused. To deliberately reproduce training later, select a new
destination rather than overwriting the verified model:

```powershell
.\backend\.venv\Scripts\python.exe -m ml_training.train_classifier --input-dir data/graph_features --artifact-dir artifacts/reproduction
```

Do not run extra experiments against the test split. Keep this fixed configuration
and threshold-selection rule. The supplied run trains one real model; ordinary
unit tests use one tiny synthetic classifier fixture, not competing production models.

## Features and fitted preprocessing

Raw feature order is fixed in `fraud_predictor.INPUT_FEATURES`:

1. `amount`
2. `card_txn_count_24h`
3. `device_txn_count_24h`
4. `card_unique_devices_7d`
5. `device_unique_cards_7d`
6. `card_avg_amount_7d`
7. `device_missing`
8. `ProductCD`
9. `M1` through `M9`, in numeric order

The first seven are numeric top-level fields. ProductCD and M1–M9 come from the
existing nested `attributes` object. The extractor explicitly selects only these
17 inputs. IDs, tokens, source amounts, synthetic dates, TransactionDT, risk scores,
extra attributes and `isFraud` never enter the feature matrix. The label is loaded
into a separate target array. Existing historical graph features already exclude
exact-time and future events; this step neither recomputes nor expands them.

Numeric null/NaN values are filled with the **training** median; an entirely absent
training column gets zero. Categorical nulls receive `__MISSING__`, then one-hot
encoding creates a separate column for each training category. An unseen category
produces zeros for that field's category columns, preserving dimensions. Invalid
numeric/infinite/negative inputs and non-string categories are rejected without
printing raw transaction values. No scaling or feature selection is performed.

The preprocessing ColumnTransformer is fitted once on training only; validation,
test and inference use `transform`. Transformed names and order are saved in
`schema.json`. Numeric values are deliberately converted to float32 for ML, so
these scores are not a substitute for Decimal accounting calculations. Stored
amounts and currency policy remain unchanged (prototype USD assumption).

## Fixed training and evaluation policy

XGBoost uses binary logistic loss, CPU histogram trees, at most 300 boosting rounds,
depth 4, learning rate 0.05, subsample 0.8, column subsample 0.9, minimum child weight
5, L2 regularization 1, random seed 42 and two worker threads. Class imbalance is
handled using `scale_pos_weight = training negatives / training positives`, computed
only from training labels. No resampling is used.

Validation `aucpr` controls early stopping with patience 30. Prediction automatically
uses the saved best iteration with the installed XGBoost sklearn interface. The
reported PR metric is sklearn **Average Precision**, a step-weighted precision-recall
summary; it is not necessarily identical to XGBoost's internal `aucpr` definition.

After fitting, select the validation threshold maximizing F1; exact ties prefer
the higher threshold. Flag a row when `risk_score >= threshold`. Then open and
evaluate test data once, after the model and threshold are fixed. The test set is
never an eval_set, preprocessing fit input or threshold-selection source.

Both validation and test metrics include Average Precision, ROC-AUC, precision,
recall, F1 and a confusion matrix with layout `[[TN, FP], [FN, TP]]`. Zero-denominator
precision/recall/F1 use zero. `training_seconds` measures model fitting only,
excluding loading, preprocessing, evaluation and artifact serialization.

## Artifacts and backend reuse

The ignored bundle contains:

- `model.ubj`: the XGBoost classifier, including best-iteration information.
- `preprocessing.joblib`: the fitted ColumnTransformer and encoders/imputers.
- `schema.json`: exact raw/transformed feature names, order and missing policies.
- `configuration.json`: parameters, threshold, best iteration, duration, input
  row counts/checksums, Python version, dependency versions and currency policy.
- `metrics.json`: measured validation and final test results.
- `manifest.json`: checksums and bundle schema version.

Artifacts are first written to a unique private staging directory. A sample of
training inputs verifies identical predictions before/after reload; publication
happens only if that check succeeds. This does not reevaluate or retune test data.
Errors remove only private staging files; original datasets are read-only. A killed
run can leave an ignored staging directory, which is not a completed bundle.

`FraudPredictor` is a small Protocol describing the `predict(rows)` operation.
`XGBoostFraudPredictor` implements it. Future FastAPI startup code can call:

```python
from ml_training.fraud_predictor import get_predictor

# Once during process startup; keep this object for all requests.
predictor = get_predictor("artifacts/fraud_classifier")
prediction = predictor.predict([transaction_with_attributes_and_graph_features])[0]
# prediction.risk_score, prediction.is_fraud, prediction.threshold
```

The single-entry cache is process-local; use a fixed configured path and restart
the process to deploy a different model. Loading performs schema/dimension and
checksum checks without training. Load only trusted local bundles: joblib uses
Python pickle, and checksums detect corruption rather than authenticate provenance.
Use the pinned environment for reloads. No API startup wiring is added in this phase.

## Verify and interpret

The full suite includes model tests; install `backend/requirements-ml.txt` first.

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q
```

Tests cover selection/leakage, missing/unseen inputs, training-only medians,
ordering, early-stopping model persistence, identical inference, cache reuse,
threshold inclusivity/ties, missing/corrupt artifacts, metrics and checked loading.
They do not read the real IEEE-CIS data or contact AWS.

## Recorded real-data run

One model was fitted on 413,378 training rows, using 88,581 validation rows. The
fixed final model was evaluated once on 88,581 test rows. Model fitting measured
47.22 seconds. Validation chose threshold **0.7617719769477844**. The run reached
the 300-round cap; validation's best iteration was 293 (zero-based), so inference
uses the first 294 rounds. This is a measured run, not a tuning experiment.

| Metric | Validation | Test |
|---|---:|---:|
| Average Precision | 0.173712 | 0.157236 |
| ROC-AUC | 0.813864 | 0.804437 |
| Precision | 0.212244 | 0.188782 |
| Recall | 0.327087 | 0.358093 |
| F1 | 0.257439 | 0.247229 |

Validation confusion matrix: `[[81846, 3693], [2047, 995]]`.
Test confusion matrix: `[[80754, 4744], [1979, 1104]]`.
The full precision values are saved locally in `artifacts/fraud_classifier/metrics.json`.
The real-data reload check produced identical predictions. All 259 automated tests
passed; one pre-existing Starlette/httpx deprecation warning remains.

Performance is modest: the test threshold catches 1,104 of 3,083 fraud cases and
produces 4,744 false alerts. This scorer is not validated for production decisions.
No configuration or threshold was changed in response to these test results.

**XGBoost** combines small decision trees, each helping correct previous errors.
It learns associations between amount, category, matching attributes and earlier
card/device activity and the supplied fraud label. A **risk score** is its estimated
fraud likelihood; class weighting means it is not a perfectly calibrated probability
or proof of fraud. **Precision** is how many flagged transactions are actually fraud;
**recall** is how much of the actual fraud is caught. A **threshold** converts a score
into a flag: lowering it generally catches more fraud and raises more false alerts.

Limitations: anonymized fingerprints can merge unrelated identities, dates/currency
are synthetic/assumed, category drift creates unknown inputs, and one historical
holdout cannot prove production reliability. Validation F1 is a simple engineering
choice, not an optimized financial cost policy. Fraud labels may also differ from
deployment outcomes. Calibration, live integration and monitoring remain future
work. No further model tuning is part of this phase.

Library references: [XGBoost sklearn/early stopping](https://xgboost.readthedocs.io/en/stable/python/python_api.html),
[XGBoost packages](https://pypi.org/project/xgboost/),
[scikit-learn packages](https://pypi.org/project/scikit-learn/).
