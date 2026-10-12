"""Offline synthetic tests; one tiny module-scoped classifier, no real dataset."""

import gzip
import hashlib
import json
import shutil
from uuid import uuid4

import numpy as np
import pytest
from xgboost import XGBClassifier

from ml_training.fraud_predictor import (
    CATEGORICAL_FEATURES, INPUT_FEATURES, NUMERIC_FEATURES, ModelError,
    XGBoostFraudPredictor, feature_matrix, feature_row, get_predictor, make_preprocessor,
)
from ml_training.prepare_dataset import PROJECT_ROOT
from ml_training.train_classifier import choose_threshold, evaluate, load_split, save_bundle


@pytest.fixture
def scratch():
    parent = PROJECT_ROOT / "data/test-fraud-classifier"
    root = parent / uuid4().hex
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        assert root.resolve().parent == parent.resolve()
        shutil.rmtree(root)


def row(amount="10.00", category="W", **changes):
    return {"amount": amount, "card_txn_count_24h": 1, "device_txn_count_24h": 2,
            "card_unique_devices_7d": 3, "device_unique_cards_7d": 4,
            "card_avg_amount_7d": "5.015", "device_missing": 0,
            "attributes": {name: category if name == "ProductCD" else None for name in CATEGORICAL_FEATURES},
            **changes}


@pytest.fixture(scope="module")
def fitted():
    rows = [row(str(i + 1), "W" if i % 2 else "C") for i in range(60)]
    labels = np.array([int(i >= 30) for i in range(60)])
    preprocessor = make_preprocessor()
    matrix = preprocessor.fit_transform(feature_matrix(rows)).astype(np.float32)
    model = XGBClassifier(n_estimators=8, max_depth=2, min_child_weight=1,
                          n_jobs=1, random_state=42, objective="binary:logistic", early_stopping_rounds=3)
    model.fit(matrix, labels, eval_set=[(matrix, labels)], verbose=False)
    return model, preprocessor, rows


def bundle(scratch, fitted, threshold=0.5):
    model, preprocessor, _ = fitted
    path = scratch / "bundle"
    save_bundle(path, model, preprocessor, {"threshold": threshold}, {"fixture": True})
    return path


def test_exact_input_selection_and_order():
    assert INPUT_FEATURES == (
        "amount", "card_txn_count_24h", "device_txn_count_24h", "card_unique_devices_7d",
        "device_unique_cards_7d", "card_avg_amount_7d", "device_missing", "ProductCD",
        "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9")
    assert feature_row(row()) == [10.0, 1.0, 2.0, 3.0, 4.0, 5.015, 0.0, "W", *([None] * 9)]
    original = row()
    reversed_order = dict(reversed(list(original.items())))
    np.testing.assert_array_equal(feature_matrix([original]), feature_matrix([reversed_order]))


def test_no_target_identifier_or_timestamp_leakage(fitted):
    model, preprocessing, _ = fitted
    predictor = XGBoostFraudPredictor(model, preprocessing, 0.5)
    original = row()
    changed = {**original, "isFraud": 1, "transaction_id": "private", "card_token": "private",
               "device_token": "private", "event_time": "2999-01-01", "TransactionDT": 10**9,
               "TransactionAmt": "999999", "risk_score": 1}
    changed["attributes"] = {**changed["attributes"], "isFraud": 1, "transaction_id": "private"}
    assert predictor.predict([original]) == predictor.predict([changed])


def test_missing_values_train_only_and_unseen_categories():
    preprocessor = make_preprocessor()
    training = [row("10"), row("30"), row(None)]
    for item in training:
        item["card_avg_amount_7d"] = None  # Entirely missing training column.
    preprocessor.fit(feature_matrix(training))
    medians_before = preprocessor.named_transformers_["numeric"].named_steps["impute"].statistics_.copy()
    validation = row(None, category="NEVER_SEEN")
    validation["card_avg_amount_7d"] = None
    matrix = preprocessor.transform(feature_matrix([validation]))
    assert np.all(np.isfinite(matrix))
    assert matrix[0, 0] == 20  # Training median, not validation statistics.
    assert matrix[0, 5] == 0
    assert matrix.shape[1] == preprocessor.transform(feature_matrix([training[0]])).shape[1]
    np.testing.assert_array_equal(medians_before, preprocessor.named_transformers_["numeric"].named_steps["impute"].statistics_)
    categories = preprocessor.named_transformers_["categorical"].named_steps["encode"].categories_
    assert "NEVER_SEEN" not in categories[0]
    assert matrix[0, len(NUMERIC_FEATURES)] == 0  # Unknown ProductCD is all zeros.


def test_save_reload_and_repeated_inference(scratch, fitted):
    path = bundle(scratch, fitted)
    model, preprocessor, rows = fitted
    expected = XGBoostFraudPredictor(model, preprocessor, 0.5).predict(rows)
    loaded = XGBoostFraudPredictor.load(path)
    assert loaded.predict(rows) == expected == loaded.predict(rows)
    assert all(0 <= result.risk_score <= 1 for result in expected)
    assert loaded.predict([]) == []
    assert loaded.predict([row(None, "UNKNOWN")])[0].risk_score >= 0


def test_cached_process_loading(scratch, fitted):
    path = bundle(scratch, fitted)
    get_predictor.cache_clear()
    try:
        assert get_predictor(str(path)) is get_predictor(str(path))
        assert get_predictor.cache_info().misses == 1
    finally:
        get_predictor.cache_clear()


def test_threshold_is_inclusive(fitted):
    model, preprocessor, rows = fitted
    initial = XGBoostFraudPredictor(model, preprocessor, 0.5)
    score = initial.predict(rows[:1])[0].risk_score
    assert XGBoostFraudPredictor(model, preprocessor, score).predict(rows[:1])[0].is_fraud
    assert not XGBoostFraudPredictor(model, preprocessor, min(1, score + 0.01)).predict(rows[:1])[0].is_fraud


@pytest.mark.parametrize("threshold", [-0.01, 1.1, float("nan"), float("inf")])
def test_invalid_threshold(threshold, fitted):
    with pytest.raises(ModelError, match="threshold"):
        XGBoostFraudPredictor(*fitted[:2], threshold)


@pytest.mark.parametrize("value", ["bad", "Infinity", "-1", "1e100", object()])
def test_invalid_numeric_inputs(value):
    with pytest.raises(ModelError, match="numeric feature"):
        feature_row(row(value))


@pytest.mark.parametrize("change", [{"attributes": None}, {"attributes": {"ProductCD": 123}}, {"device_missing": 2}])
def test_invalid_categorical_and_flag_inputs(change):
    with pytest.raises(ModelError):
        feature_row(row(**change))


def test_basic_metrics_and_validation_threshold():
    labels, scores = [0, 0, 1, 1], [0.1, 0.6, 0.4, 0.9]
    metrics = evaluate(labels, scores, 0.5)
    assert metrics["average_precision"] == pytest.approx(5 / 6)
    assert metrics["roc_auc"] == 0.75
    assert metrics["precision"] == metrics["recall"] == metrics["f1"] == 0.5
    assert metrics["confusion_matrix"] == [[1, 1], [1, 1]]
    assert choose_threshold(labels, scores) == 0.4
    assert evaluate(labels, scores, 0.4)["f1"] == pytest.approx(0.8)


def test_threshold_ties_choose_higher_threshold():
    assert choose_threshold([1, 0, 0, 1], [1, 0.8, 0.7, 0.6]) == 1


@pytest.mark.parametrize("labels,scores", [([0, 0], [0.1, 0.2]), ([0, 1], [0.1, float("nan")]),
                                          ([0, 2], [0.1, 0.9]), ([0, 1], [0.1, 1.1]), ([0, 1], [0.1])])
def test_bad_metric_inputs(labels, scores):
    with pytest.raises(ModelError):
        evaluate(labels, scores, 0.5)


@pytest.mark.parametrize("problem", ["missing", "bad-manifest", "corrupt-model", "wrong-schema", "bad-threshold"])
def test_missing_or_malformed_artifacts(scratch, fitted, problem):
    path = bundle(scratch, fitted)
    if problem == "missing":
        (path / "schema.json").unlink()
    elif problem == "bad-manifest":
        (path / "manifest.json").write_text("invalid", encoding="utf-8")
    elif problem == "corrupt-model":
        (path / "model.ubj").write_bytes(b"invalid")
    else:
        name = "schema.json" if problem == "wrong-schema" else "configuration.json"
        data = json.loads((path / name).read_text())
        if problem == "wrong-schema":
            data["input_features"] = list(reversed(data["input_features"]))
        else:
            data["threshold"] = 2
        (path / name).write_text(json.dumps(data), encoding="utf-8")
        manifest = json.loads((path / "manifest.json").read_text())
        manifest["sha256"][name] = hashlib.sha256((path / name).read_bytes()).hexdigest()
        (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ModelError):
        XGBoostFraudPredictor.load(path)


def test_loader_checks_rows_and_hash_without_leaking_metadata(scratch):
    rows = [row(isFraud=0, transaction_id="not-a-feature"), row("50", isFraud=1)]
    path = scratch / "train.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for item in rows:
            handle.write(json.dumps(item) + "\n")
    report = {"rows_per_split": {"train": 2}, "output_sha256": {"train": hashlib.sha256(path.read_bytes()).hexdigest()}}
    matrix, labels, sample = load_split(scratch, "train", report)
    assert matrix.shape == (2, 17)
    assert labels.tolist() == [0, 1]
    assert "transaction_id" not in sample[0] and "isFraud" not in sample[0]
    report["rows_per_split"]["train"] = 3
    with pytest.raises(ModelError, match="row count"):
        load_split(scratch, "train", report)
    report["output_sha256"]["train"] = "wrong"
    with pytest.raises(ModelError, match="checksum"):
        load_split(scratch, "train", report)
