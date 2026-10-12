"""Synthetic artifact stubs only: no training, real datasets or saved real bundle."""

import hashlib
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest
from pydantic import ValidationError

from app.graph.models import GraphFeatures
from app.ml import fraud_predictor as runtime
from app.schemas.fraud_inference import InferenceTransaction, TransactionModelAttributes
from app.services.fraud_inference_service import (
    _cached_service, get_fraud_inference_service, model_input,
)


def transaction(**changes):
    return InferenceTransaction(amount="80.00", currency="USD", device_missing=False, **changes)


def history(**changes):
    return GraphFeatures(**{"card_txn_count_24h": 2, "device_txn_count_24h": 1,
                            "card_unique_devices_7d": 3, "device_unique_cards_7d": 4,
                            "card_avg_amount_7d": Decimal("12.345"), **changes})


def refresh_manifest(path):
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in path.iterdir() if p.name != "manifest.json"}
    (path / "manifest.json").write_text(json.dumps({"version": 1, "sha256": hashes}), encoding="utf-8")


def change_json(path, name, update):
    content = json.loads((path / name).read_text())
    content.update(update)
    (path / name).write_text(json.dumps(content), encoding="utf-8")
    refresh_manifest(path)


@pytest.fixture
def artifacts(monkeypatch):
    parent = Path(__file__).resolve().parents[2] / "data/test-fraud-inference"
    root = parent / uuid4().hex
    root.mkdir(parents=True)
    loads = {"model": 0, "preprocessing": 0}

    class Preprocessor:
        n_features_in_ = 17

        def get_feature_names_out(self):
            return np.array([f"feature_{i}" for i in range(40)])

        def transform(self, matrix):
            self.last_matrix = matrix.copy()
            return np.array([[*map(float, row[:7]), *([0] * 33)] for row in matrix])

    class Model:
        n_features_in_ = 40
        classes_ = np.array([0, 1])
        objective = "binary:logistic"
        best_iteration = 1

        def load_model(self, path):
            loads["model"] += 1

        def set_params(self, **kwargs):
            return self

        def predict_proba(self, matrix):
            scores = np.clip(matrix[:, 0] / 100, 0, 1)
            return np.column_stack((1 - scores, scores))

    preprocessor, model = Preprocessor(), Model()

    def load_preprocessor(path):
        loads["preprocessing"] += 1
        return preprocessor

    monkeypatch.setattr(runtime.joblib, "load", load_preprocessor)
    monkeypatch.setattr(runtime, "XGBClassifier", lambda: model)
    (root / "model.ubj").write_bytes(b"synthetic model stub, never trained")
    (root / "preprocessing.joblib").write_bytes(b"synthetic preprocessing stub")
    (root / "schema.json").write_text(json.dumps(runtime.feature_schema(preprocessor)), encoding="utf-8")
    (root / "configuration.json").write_text(json.dumps({"threshold": 0.75,
        "input_features": list(runtime.INPUT_FEATURES), "best_iteration": 1,
        "currency_policy": "Phase 3.1 assumed USD; no currency conversion"}), encoding="utf-8")
    (root / "metrics.json").write_text("{}", encoding="utf-8")
    refresh_manifest(root)
    _cached_service.cache_clear()
    runtime.get_predictor.cache_clear()
    try:
        yield SimpleNamespace(path=root, loads=loads, preprocessor=preprocessor, model=model)
    finally:
        _cached_service.cache_clear()
        runtime.get_predictor.cache_clear()
        assert root.resolve().parent == parent.resolve()
        shutil.rmtree(root)


def test_load_and_cached_reuse_with_canonical_paths(artifacts):
    first = get_fraud_inference_service(artifacts.path)
    assert first is get_fraud_inference_service(artifacts.path / ".")
    assert artifacts.loads == {"model": 1, "preprocessing": 1}
    assert first.predict(transaction(), history()) == first.predict(transaction(), history())
    assert artifacts.loads == {"model": 1, "preprocessing": 1}


def test_concurrent_startup_loads_once(artifacts):
    with ThreadPoolExecutor(max_workers=8) as executor:
        services = list(executor.map(lambda _: get_fraud_inference_service(artifacts.path), range(16)))
    assert all(service is services[0] for service in services)
    assert artifacts.loads == {"model": 1, "preprocessing": 1}


def test_mapping_order_threshold_result_and_no_mutation(artifacts):
    service = get_fraud_inference_service(artifacts.path)
    tx = transaction(attributes={"M9": "F", "ProductCD": "W", "M1": "T"})
    graph = history()
    before = (tx.model_dump(), graph)
    result = service.predict(tx, graph)
    assert artifacts.preprocessor.last_matrix[0].tolist() == [
        80.0, 2.0, 1.0, 3.0, 4.0, 12.345, 0.0, "W", "T", *([None] * 7), "F"]
    assert result.risk_level == "HIGH" and 0 <= result.risk_score <= 1
    assert result.model_version.startswith("xgb-v1-")
    assert set(result.model_dump()) == {"risk_score", "risk_level", "model_version"}
    assert before == (tx.model_dump(), graph)
    assert service.predict(transaction().model_copy(update={"amount": Decimal("75")}), graph).risk_level == "HIGH"
    assert service.predict(transaction().model_copy(update={"amount": Decimal("74")}), graph).risk_level == "LOW"


def test_missing_categories_preserved_as_null_and_unknown_strings_accepted(artifacts):
    service = get_fraud_inference_service(artifacts.path)
    service.predict(transaction(), history())
    assert artifacts.preprocessor.last_matrix[0, 7:].tolist() == [None] * 10
    service.predict(transaction(attributes={"ProductCD": "unseen-product"}), history())
    assert artifacts.preprocessor.last_matrix[0, 7] == "unseen-product"


@pytest.mark.parametrize("attributes", [{"ProductCD": 1}, {"M1": True}, {"M2": []},
                                         {"M3": ""}, {"M4": "__MISSING__"}, {"isFraud": 1}])
def test_bad_categorical_inputs_rejected(attributes):
    with pytest.raises(ValidationError):
        TransactionModelAttributes(**attributes)


@pytest.mark.parametrize("changes", [{"currency": "INR"}, {"device_missing": "false"},
                                     {"amount": "NaN"}, {"amount": "-1"}, {"card_token": "private"}])
def test_transaction_input_types_and_currency_guard(changes):
    fields = {"amount": "10", "currency": "USD", "device_missing": False, **changes}
    with pytest.raises(ValidationError):
        InferenceTransaction(**fields)


@pytest.mark.parametrize("changes", [{"card_txn_count_24h": True}, {"card_txn_count_24h": "1"},
                                     {"device_txn_count_24h": -1}, {"card_unique_devices_7d": 1.5},
                                     {"card_avg_amount_7d": 1.0}, {"card_avg_amount_7d": Decimal("NaN")},
                                     {"card_avg_amount_7d": Decimal("-1")}])
def test_invalid_graph_feature_types(changes):
    with pytest.raises(runtime.ModelError):
        model_input(transaction(), history(**changes))


def test_inconsistent_features_and_missing_device():
    missing_device = InferenceTransaction(amount="10", currency="USD", device_missing=True)
    with pytest.raises(runtime.ModelError, match="absent device"):
        model_input(missing_device, history())
    mapped = model_input(missing_device, history(device_txn_count_24h=0, device_unique_cards_7d=0))
    assert mapped["device_missing"] == 1
    with pytest.raises(runtime.ModelError, match="prior device-card"):
        model_input(transaction(), history(device_unique_cards_7d=0))
    with pytest.raises(runtime.ModelError, match="positive historical average"):
        model_input(transaction(), history(card_avg_amount_7d=Decimal(0)))
    assert model_input(missing_device, GraphFeatures())["card_avg_amount_7d"] == 0


def test_unvalidated_inputs_rejected():
    with pytest.raises(runtime.ModelError):
        model_input({}, GraphFeatures())
    with pytest.raises(runtime.ModelError):
        model_input(transaction(), {})


@pytest.mark.parametrize("changes", [{"currency": "INR"}, {"amount": Decimal("-1")}])
def test_copied_models_cannot_bypass_inference_validation(artifacts, changes):
    # Pydantic model_copy does not validate updates; the service must revalidate.
    copied = transaction().model_copy(update=changes)
    service = get_fraud_inference_service(artifacts.path)
    with pytest.raises(runtime.ModelError, match="Malformed inference inputs"):
        service.predict(copied, history())
    assert not hasattr(artifacts.preprocessor, "last_matrix")


@pytest.mark.parametrize("problem", ["missing", "corrupt", "schema", "configured-order", "dependency", "best-iteration", "currency"])
def test_artifact_failures_are_clear_without_fallback(artifacts, problem):
    if problem == "missing":
        (artifacts.path / "model.ubj").unlink()
    elif problem == "corrupt":
        (artifacts.path / "preprocessing.joblib").write_bytes(b"corrupted")
    elif problem == "schema":
        change_json(artifacts.path, "schema.json", {"input_features": []})
    else:
        updates = {"configured-order": {"input_features": []}, "dependency": {"dependencies": {"numpy": "0.invalid"}},
                   "best-iteration": {"best_iteration": 99}, "currency": {"currency_policy": "unknown"}}
        change_json(artifacts.path, "configuration.json", updates[problem])
    with pytest.raises(runtime.ModelError):
        get_fraud_inference_service(artifacts.path)


@pytest.mark.parametrize("problem", ["dimensions", "objective"])
def test_incompatible_classifier_rejected(artifacts, problem):
    if problem == "dimensions":
        artifacts.model.n_features_in_ = 39
    else:
        artifacts.model.objective = "reg:squarederror"
    with pytest.raises(runtime.ModelError):
        get_fraud_inference_service(artifacts.path)


@pytest.mark.parametrize("score", [float("nan"), -0.1, 1.1])
def test_invalid_output_has_no_fabricated_fallback(artifacts, monkeypatch, score):
    service = get_fraud_inference_service(artifacts.path)
    monkeypatch.setattr(service._predictor, "predict", lambda _: [runtime.FraudPrediction(score, False, 0.75)])
    with pytest.raises(runtime.ModelError):
        service.predict(transaction(), history())


def test_prediction_failure_is_sanitized(artifacts, monkeypatch):
    service = get_fraud_inference_service(artifacts.path)

    def fail(_):
        raise RuntimeError("private-card-token")

    monkeypatch.setattr(service._predictor, "predict", fail)
    with pytest.raises(runtime.ModelError) as failure:
        service.predict(transaction(), history())
    assert str(failure.value) == "Model inference failed"


def test_no_database_or_graph_side_effects(artifacts, monkeypatch):
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session
    from app.graph.in_memory import InMemoryGraphRepository
    from app.graph.neptune import NeptuneGraphRepository

    def forbidden(*args, **kwargs):
        pytest.fail("Inference attempted database or graph access")

    for name in ("add", "commit", "execute", "flush"):
        monkeypatch.setattr(Session, name, forbidden)
    monkeypatch.setattr(Engine, "connect", forbidden)
    monkeypatch.setattr(InMemoryGraphRepository, "upsert_transaction", forbidden)
    monkeypatch.setattr(NeptuneGraphRepository, "upsert_transaction", forbidden)
    result = get_fraud_inference_service(artifacts.path).predict(transaction(), history())
    assert result.risk_level == "HIGH"
