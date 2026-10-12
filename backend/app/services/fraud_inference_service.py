"""Synchronous, read-only inference. No SQL, graph adapter or alert dependencies."""

import math
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from ..graph.models import GraphFeatures
from ..ml.fraud_predictor import ModelError, XGBoostFraudPredictor, get_predictor
from ..schemas.fraud_inference import (
    FraudInferenceResult, InferenceTransaction, ValidatedGraphFeatures,
)


DEFAULT_ARTIFACT_DIR = Path(__file__).resolve().parents[3] / "artifacts/fraud_classifier"
_load_lock = RLock()


def model_input(transaction: InferenceTransaction, features: GraphFeatures) -> dict:
    """Build the exact training layout after validating types and consistency."""
    if not isinstance(transaction, InferenceTransaction) or not isinstance(features, GraphFeatures):
        raise ModelError("Expected InferenceTransaction and GraphFeatures inputs")
    try:
        # Revalidate even constructed/copied objects that bypassed normal validators.
        transaction = InferenceTransaction.model_validate(transaction.model_dump())
        graph = ValidatedGraphFeatures.model_validate(asdict(features))
    except ValidationError:
        raise ModelError("Malformed inference inputs") from None
    if transaction.device_missing and (graph.device_txn_count_24h or graph.device_unique_cards_7d):
        raise ModelError("An absent device must have zero device-history features")
    if graph.device_txn_count_24h and not graph.device_unique_cards_7d:
        raise ModelError("Device transaction count requires prior device-card history")
    if graph.card_avg_amount_7d == 0 and (graph.card_txn_count_24h or graph.card_unique_devices_7d):
        raise ModelError("Card activity requires a positive historical average")
    return {"amount": transaction.amount, **graph.model_dump(),
            "device_missing": int(transaction.device_missing),
            "attributes": transaction.attributes.model_dump()}


class FraudInferenceService:
    """Hold one loaded predictor. Return two risk levels using its saved threshold.

    LOW means below the threshold, not proven legitimate. No arbitrary second
    threshold/medium level is introduced. This class does not fetch graph history
    or persist predictions; those responsibilities belong to the Phase 4 workflow.
    """

    def __init__(self, predictor: XGBoostFraudPredictor):
        if not predictor.model_version or predictor.training_currency != "USD":
            raise ModelError("Inference requires versioned model artifacts with the known USD currency policy")
        self._predictor = predictor

    def predict(self, transaction: InferenceTransaction, features: GraphFeatures) -> FraudInferenceResult:
        row = model_input(transaction, features)
        try:
            predictions = self._predictor.predict([row])
            if len(predictions) != 1:
                raise ModelError("Classifier must return exactly one prediction")
            score = float(predictions[0].risk_score)
            if not math.isfinite(score) or not 0 <= score <= 1:
                raise ModelError("Classifier returned an invalid risk score")
            return FraudInferenceResult(risk_score=score,
                risk_level="HIGH" if score >= self._predictor.threshold else "LOW",
                model_version=self._predictor.model_version)
        except ModelError:
            raise
        except Exception:
            # Never include raw input values or low-level exception text in logs/responses.
            raise ModelError("Model inference failed") from None


@lru_cache(maxsize=1)
def _cached_service(artifact_dir: str) -> FraudInferenceService:
    return FraudInferenceService(get_predictor(artifact_dir))


def get_fraud_inference_service(artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR) -> FraudInferenceService:
    """Load once during startup; canonical paths and a lock serialize first load.

    Keep a fixed bundle per process. Neither cache is invalidated per prediction;
    restart for a new model. No model is loaded when the service module is imported.
    """
    with _load_lock:
        return _cached_service(str(Path(artifact_dir).resolve()))
