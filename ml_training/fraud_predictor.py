"""Compatibility import for the runtime predictor, now owned by the backend.

Backend commands run from backend/ (package app); offline commands run from the
repository root (package backend.app). Both use the same implementation file.
No model or preprocessing artifact references custom classes from this module.
"""

try:
    from app.ml.fraud_predictor import (
        CATEGORICAL_FEATURES, INPUT_FEATURES, NUMERIC_FEATURES, SCHEMA_VERSION,
        FraudPrediction, FraudPredictor, ModelError, XGBoostFraudPredictor,
        feature_matrix, feature_row, feature_schema, get_predictor, make_preprocessor,
    )
except ModuleNotFoundError as exc:
    if exc.name != "app":
        raise  # Do not hide missing optional ML dependencies.
    from backend.app.ml.fraud_predictor import (
        CATEGORICAL_FEATURES, INPUT_FEATURES, NUMERIC_FEATURES, SCHEMA_VERSION,
        FraudPrediction, FraudPredictor, ModelError, XGBoostFraudPredictor,
        feature_matrix, feature_row, feature_schema, get_predictor, make_preprocessor,
    )
