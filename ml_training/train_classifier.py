"""One graph-aware classifier, one validation threshold, one final test evaluation."""

import argparse
import gzip
import hashlib
import importlib.metadata
import json
import platform
import shutil
import sys
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import joblib
import numpy as np
from sklearn.metrics import (average_precision_score, confusion_matrix, f1_score,
                             precision_recall_curve, precision_score, recall_score, roc_auc_score)
from xgboost import XGBClassifier

from ml_training.fraud_predictor import (INPUT_FEATURES, ModelError, XGBoostFraudPredictor,
                                        feature_row, feature_schema, make_preprocessor)
from ml_training.prepare_dataset import PROJECT_ROOT


MODEL_CONFIG = {"objective": "binary:logistic", "tree_method": "hist", "n_estimators": 300,
                "max_depth": 4, "learning_rate": 0.05, "subsample": 0.8,
                "colsample_bytree": 0.9, "min_child_weight": 5, "reg_lambda": 1,
                "random_state": 42, "n_jobs": 2, "eval_metric": "aucpr", "early_stopping_rounds": 30}


def checked_labels_scores(labels, scores):
    labels, scores = np.asarray(labels), np.asarray(scores, dtype=float)
    if (labels.ndim != 1 or scores.shape != labels.shape or not len(labels)
            or not np.all(np.isin(labels, [0, 1])) or len(np.unique(labels)) != 2
            or not np.all(np.isfinite(scores)) or np.any((scores < 0) | (scores > 1))):
        raise ModelError("Metrics require both binary classes and finite probabilities in [0,1]")
    return labels, scores


def choose_threshold(labels, scores) -> float:
    """Maximize validation F1; exact ties prefer the higher threshold/fewer alerts."""
    labels, scores = checked_labels_scores(labels, scores)
    precision, recall, thresholds = precision_recall_curve(labels, scores)
    numerator = 2 * precision[:-1] * recall[:-1]
    denominator = precision[:-1] + recall[:-1]
    f1 = np.divide(numerator, denominator, out=np.zeros_like(numerator), where=denominator != 0)
    return float(thresholds[np.flatnonzero(f1 == f1.max())[-1]])


def evaluate(labels, scores, threshold: float) -> dict:
    labels, scores = checked_labels_scores(labels, scores)
    if not np.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ModelError("Invalid classification threshold")
    predicted = scores >= threshold
    return {"average_precision": float(average_precision_score(labels, scores)),
            "roc_auc": float(roc_auc_score(labels, scores)),
            "precision": float(precision_score(labels, predicted, zero_division=0)),
            "recall": float(recall_score(labels, predicted, zero_division=0)),
            "f1": float(f1_score(labels, predicted, zero_division=0)),
            "confusion_matrix": confusion_matrix(labels, predicted, labels=[0, 1]).tolist(),
            "rows": len(labels), "positive_rows": int(np.sum(labels)), "threshold": float(threshold)}


def load_split(directory: Path, split: str, report: dict):
    """Stream JSON Lines into selected arrays, discarding all identifiers/metadata.

    Verify the Phase 3.2 report's checksum and row count. Test data is not opened
    until training, early stopping and validation threshold selection are done.
    """
    path = directory / f"{split}.jsonl.gz"
    with path.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != report["output_sha256"][split]:
            raise ModelError(f"{split}: Phase 3.2 checksum mismatch")
    count = report["rows_per_split"][split]
    if type(count) is not int or count <= 0:
        raise ModelError(f"{split}: expected a nonempty partition")
    matrix, labels = np.empty((count, len(INPUT_FEATURES)), dtype=object), np.empty(count, dtype=np.uint8)
    sample = []
    actual = 0
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for actual, line in enumerate(handle, 1):
            row = json.loads(line)
            if actual > count or type(row.get("isFraud")) is not int or row["isFraud"] not in (0, 1):
                raise ModelError(f"{split}: invalid row count or binary target")
            matrix[actual - 1] = feature_row(row)
            labels[actual - 1] = row["isFraud"]
            if len(sample) < 16:
                # Sample holds only predictor fields, never tokens or identifiers.
                sample.append({key: row.get(key) for key in (*INPUT_FEATURES[:7], "attributes")})
    if actual != count or len(np.unique(labels)) != 2:
        raise ModelError(f"{split}: row count mismatch or a missing target class")
    return matrix, labels, sample


def save_bundle(directory: Path, model, preprocessor, configuration: dict, metrics: dict):
    """Write into a new directory. Caller publishes it after reload verification."""
    directory.mkdir(exist_ok=False)
    model.save_model(directory / "model.ubj")
    joblib.dump(preprocessor, directory / "preprocessing.joblib")
    for name, content in (("schema.json", feature_schema(preprocessor)),
                          ("configuration.json", configuration), ("metrics.json", metrics)):
        (directory / name).write_text(json.dumps(content, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    checksums = {}
    for path in sorted(directory.iterdir()):
        with path.open("rb") as handle:
            checksums[path.name] = hashlib.file_digest(handle, "sha256").hexdigest()
    (directory / "manifest.json").write_text(json.dumps({"version": 1, "sha256": checksums}, indent=2) + "\n", encoding="utf-8")


def train(input_dir: Path, artifact_dir: Path) -> dict:
    input_dir, artifact_dir = Path(input_dir).resolve(), Path(artifact_dir).resolve()
    if artifact_dir.exists() or input_dir.is_relative_to(artifact_dir):
        raise ModelError("Artifact directory already exists or overlaps input; choose a new directory")
    report = json.loads((input_dir / "report.json").read_text(encoding="utf-8"))
    print("Loading training and validation partitions...", flush=True)
    raw_train, y_train, sample = load_split(input_dir, "train", report)
    raw_validation, y_validation, _ = load_split(input_dir, "validation", report)
    preprocessor = make_preprocessor()
    x_train = preprocessor.fit_transform(raw_train).astype(np.float32)
    x_validation = preprocessor.transform(raw_validation).astype(np.float32)
    del raw_train, raw_validation
    positives = int(y_train.sum())
    configuration = {"model_parameters": {**MODEL_CONFIG, "scale_pos_weight": (len(y_train) - positives) / positives},
        "threshold_policy": "maximum validation F1; highest threshold on exact ties",
        "python_version": platform.python_version(),
        "dependencies": {name: importlib.metadata.version(name) for name in ("xgboost", "scikit-learn", "numpy", "scipy", "joblib")},
        "source_sha256": report["output_sha256"], "input_features": list(INPUT_FEATURES),
        "rows_per_split": report["rows_per_split"],
        "currency_policy": "Phase 3.1 assumed USD; no currency conversion"}
    model = XGBClassifier(**configuration["model_parameters"])
    print("Fitting the single classifier...", flush=True)
    started = perf_counter()
    model.fit(x_train, y_train, eval_set=[(x_validation, y_validation)], verbose=False)
    duration = perf_counter() - started
    validation_scores = model.predict_proba(x_validation)[:, 1]
    threshold = choose_threshold(y_validation, validation_scores)
    configuration.update(threshold=threshold, best_iteration=int(model.best_iteration), training_seconds=duration)
    metrics = {"validation": evaluate(y_validation, validation_scores, threshold)}
    del x_train, x_validation
    print("Model and threshold fixed; evaluating test partition once...", flush=True)
    raw_test, y_test, _ = load_split(input_dir, "test", report)
    x_test = preprocessor.transform(raw_test).astype(np.float32)
    del raw_test
    test_scores = model.predict_proba(x_test)[:, 1]
    metrics["test"] = evaluate(y_test, test_scores, threshold)
    artifact_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = artifact_dir.parent / f".classifier-{uuid4().hex}"
    try:
        save_bundle(staging, model, preprocessor, configuration, metrics)
        before = XGBoostFraudPredictor(model, preprocessor, threshold).predict(sample)
        after = XGBoostFraudPredictor.load(staging).predict(sample)
        if before != after:
            raise ModelError("Saved model predictions differ after reload")
        if artifact_dir.exists():
            raise ModelError("Artifact directory appeared during training; refusing to replace it")
        staging.rename(artifact_dir)
    finally:
        if staging.exists():
            assert staging.resolve().parent == artifact_dir.parent
            shutil.rmtree(staging)
    return {"configuration": configuration, "metrics": metrics, "reload_predictions_identical": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=PROJECT_ROOT / "data/graph_features")
    parser.add_argument("--artifact-dir", type=Path, default=PROJECT_ROOT / "artifacts/fraud_classifier")
    args = parser.parse_args(argv)
    try:
        result = train(args.input_dir, args.artifact_dir)
    except (ModelError, OSError, ValueError, KeyError) as exc:
        message = str(exc) if isinstance(exc, ModelError) else f"Training failed ({type(exc).__name__}); check local inputs, dependencies and paths"
        print(message, file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
