"""Explicit model inputs, fitted preprocessing, and reusable process-local inference."""

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Protocol, Sequence

import joblib
import numpy as np
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder
from xgboost import XGBClassifier


NUMERIC_FEATURES = ("amount", "card_txn_count_24h", "device_txn_count_24h",
                    "card_unique_devices_7d", "device_unique_cards_7d",
                    "card_avg_amount_7d", "device_missing")
CATEGORICAL_FEATURES = ("ProductCD",) + tuple(f"M{i}" for i in range(1, 10))
INPUT_FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES
SCHEMA_VERSION = 1


class ModelError(ValueError):
    """Safe model/input failure, without raw transaction data in the message."""


def feature_row(row: Mapping) -> list:
    """Whitelist predictors; ignore targets, identifiers, tokens and timestamps.

    Numeric strings/Decimal are converted deliberately to ML float values.
    Null/NaN numeric inputs use the fitted training median. Categorical nulls
    use a fixed missing marker; unseen strings produce all-zero one-hot entries.
    Invalid nonnumeric/infinite inputs are rejected rather than silently coerced.
    """
    if not isinstance(row, Mapping):
        raise ModelError("Prediction input must be a mapping")
    values = []
    for name in NUMERIC_FEATURES:
        value = row.get(name)
        try:
            value = np.nan if value is None else float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ModelError(f"Invalid numeric feature: {name}") from exc
        if np.isinf(value) or (not np.isnan(value) and (value < 0 or value > float(np.finfo(np.float32).max))):
            raise ModelError(f"Invalid numeric feature: {name}")
        if name == "device_missing" and not np.isnan(value) and value not in (0, 1):
            raise ModelError("device_missing must be 0, 1 or null")
        values.append(value)
    attributes = row.get("attributes", {})
    if not isinstance(attributes, Mapping):
        raise ModelError("attributes must be a mapping")
    for name in CATEGORICAL_FEATURES:
        value = attributes.get(name)
        if value is not None and not isinstance(value, str):
            raise ModelError(f"Invalid categorical feature: {name}")
        values.append(value)
    return values


def feature_matrix(rows: Sequence[Mapping]) -> np.ndarray:
    return np.asarray([feature_row(row) for row in rows], dtype=object).reshape((-1, len(INPUT_FEATURES)))


def make_preprocessor() -> ColumnTransformer:
    """Construct an unfitted pipeline; fit exactly once on training rows only."""
    numeric = Pipeline([("impute", SimpleImputer(strategy="median", keep_empty_features=True))])
    categorical = Pipeline([
        ("impute", SimpleImputer(missing_values=None, strategy="constant", fill_value="__MISSING__", keep_empty_features=True)),
        ("encode", OneHotEncoder(handle_unknown="ignore", sparse_output=False, dtype=np.float32)),
    ])
    return ColumnTransformer([
        ("numeric", numeric, list(range(len(NUMERIC_FEATURES)))),
        ("categorical", categorical, list(range(len(NUMERIC_FEATURES), len(INPUT_FEATURES)))),
    ], remainder="drop")


def feature_schema(preprocessor) -> dict:
    return {"version": SCHEMA_VERSION, "input_features": list(INPUT_FEATURES),
            "numeric_features": list(NUMERIC_FEATURES), "categorical_features": list(CATEGORICAL_FEATURES),
            "transformed_features": preprocessor.get_feature_names_out().tolist(),
            "numeric_missing_policy": "training median; all-missing training column becomes zero",
            "categorical_missing_policy": "__MISSING__", "unknown_category_policy": "all-zero encoding"}


@dataclass(frozen=True)
class FraudPrediction:
    risk_score: float
    is_fraud: bool
    threshold: float


class FraudPredictor(Protocol):
    def predict(self, rows: Sequence[Mapping]) -> list[FraudPrediction]: ...


class XGBoostFraudPredictor:
    """Instantiate at startup and reuse. Loading never fits or retrains anything.

    Load only locally trusted artifacts: joblib preprocessing uses Python pickle.
    Checksums detect corruption, not malicious replacement of the whole bundle.
    """

    def __init__(self, model, preprocessor, threshold: float):
        if not np.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ModelError("Classification threshold must be between 0 and 1")
        self.model, self.preprocessor, self.threshold = model, preprocessor, float(threshold)

    @classmethod
    def load(cls, directory: Path):
        directory = Path(directory)
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            required = {"model.ubj", "preprocessing.joblib", "schema.json", "configuration.json", "metrics.json"}
            if manifest["version"] != SCHEMA_VERSION or set(manifest["sha256"]) != required:
                raise ModelError("Incompatible artifact manifest")
            for name in required:
                with (directory / name).open("rb") as handle:
                    if hashlib.file_digest(handle, "sha256").hexdigest() != manifest["sha256"][name]:
                        raise ModelError("Artifact checksum mismatch")
            schema = json.loads((directory / "schema.json").read_text(encoding="utf-8"))
            configuration = json.loads((directory / "configuration.json").read_text(encoding="utf-8"))
            preprocessor = joblib.load(directory / "preprocessing.joblib")
            if schema != feature_schema(preprocessor):
                raise ModelError("Incompatible feature schema/order")
            model = XGBClassifier()
            model.load_model(directory / "model.ubj")
            model.set_params(n_jobs=configuration.get("model_parameters", {}).get("n_jobs", 1))
            if model.n_features_in_ != len(schema["transformed_features"]) or list(model.classes_) != [0, 1]:
                raise ModelError("Incompatible classifier dimensions/classes")
            return cls(model, preprocessor, configuration["threshold"])
        except ModelError:
            raise
        except Exception as exc:
            raise ModelError("Missing, malformed or incompatible trusted model artifacts") from exc

    def predict(self, rows: Sequence[Mapping]) -> list[FraudPrediction]:
        if not rows:
            return []
        matrix = self.preprocessor.transform(feature_matrix(rows)).astype(np.float32)
        scores = self.model.predict_proba(matrix)[:, 1]
        if not np.all(np.isfinite(scores)) or np.any((scores < 0) | (scores > 1)):
            raise ModelError("Classifier returned invalid probabilities")
        return [FraudPrediction(float(score), bool(score >= self.threshold), self.threshold) for score in scores]


@lru_cache(maxsize=1)
def get_predictor(artifact_dir: str) -> XGBoostFraudPredictor:
    """Call once at process startup with a fixed bundle path; reuse per request.

    Cache is per Python process; replacing files does not hot-reload the model.
    Future FastAPI startup wiring is deliberately outside this phase.
    """
    return XGBoostFraudPredictor.load(Path(artifact_dir))
