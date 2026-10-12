"""Read-only orchestration: validate, guard freshness, read history, infer."""

from contextlib import contextmanager
from dataclasses import asdict
from datetime import timedelta
from enum import Enum
from typing import ContextManager, Iterator, Protocol

from pydantic import ValidationError

from app.graph.models import GraphFeatures
from app.graph.repository import GraphRepository
from app.ml.fraud_predictor import ModelError
from app.schemas.fraud_inference import InferenceTransaction, TransactionModelAttributes
from app.schemas.fraud_scoring import (
    FraudScoringRequest, FraudScoringResult, HistoricalGraphMetadata,
)
from app.services.fraud_inference_service import FraudInferenceService
from app.services.risk_indicators import observed_risk_indicators


class GraphFreshness(Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class GraphFreshnessChecker(Protocol):
    def guard(self, request: FraudScoringRequest) -> ContextManager[GraphFreshness]:
        """Attest to this request's relevant seven-day history, during the guard.

        FRESH requires all relevant earlier source events to be visible in the
        injected graph, card amount history to be USD, and consistent worker
        state. Pending/retrying, processing (including expired leases), failed,
        or missing sync jobs must block if completeness cannot be proven.
        A maximum synchronized timestamp alone cannot establish this.

        A future persistent implementation must coordinate source arrivals,
        corrections and concurrent workers or use a coherent snapshot so the
        attestation stays valid through the graph read. Never suppress errors.
        This phase deliberately provides no SQL/Neptune implementation.
        """
        ...


class UnknownGraphFreshnessChecker:
    """Fail closed until a real source/graph consistency policy is injected."""

    @contextmanager
    def guard(self, request: FraudScoringRequest) -> Iterator[GraphFreshness]:
        yield GraphFreshness.UNKNOWN


class FraudScoringError(ValueError):
    """Sanitized orchestration failure; never a substitute score."""


class GraphFreshnessError(FraudScoringError):
    pass


class GraphHistoryError(FraudScoringError):
    pass


class FraudScoringService:
    def __init__(self, graph: GraphRepository, inference: FraudInferenceService,
                 freshness: GraphFreshnessChecker | None = None):
        self.graph = graph
        self.inference = inference
        self.freshness = freshness if freshness is not None else UnknownGraphFreshnessChecker()

    def score(self, request: FraudScoringRequest) -> FraudScoringResult:
        """One graph lookup after freshness verification; no writes or retries.

        Unknown/stale freshness is rejected BEFORE graph/model access. No graph
        lookup occurs for rejected requests; successful attempts use exactly one.
        The current transaction is never inserted into the graph here.
        """
        if not isinstance(request, FraudScoringRequest):
            raise FraudScoringError("Expected a validated fraud scoring request")
        try:
            # Frozen Pydantic objects can still be copied without validation.
            request = FraudScoringRequest.model_validate(request.model_dump(warnings=False))
        except ValidationError:
            raise FraudScoringError("Malformed fraud scoring request") from None

        try:
            with self.freshness.guard(request) as status:
                if status is not GraphFreshness.FRESH:
                    raise GraphFreshnessError("Graph freshness is stale or unknown")
                try:
                    features = self.graph.get_historical_features(
                        request.card_token, request.device_token, request.event_time,
                    )
                except Exception:
                    raise GraphHistoryError("Historical graph lookup failed") from None
                if not isinstance(features, GraphFeatures):
                    raise GraphHistoryError("Historical graph returned invalid features")
                try:
                    metadata = HistoricalGraphMetadata(
                        **asdict(features), cutoff=request.event_time,
                        window_start_24h=request.event_time - timedelta(hours=24),
                        window_start_7d=request.event_time - timedelta(days=7),
                    )
                except (ValidationError, OverflowError):
                    raise GraphHistoryError("Historical graph returned invalid features") from None
                attributes = TransactionModelAttributes.model_validate(
                    {name: getattr(request, name) for name in TransactionModelAttributes.model_fields}
                )
                transaction = InferenceTransaction(
                    amount=request.amount, currency=request.currency,
                    device_missing=request.device_token is None, attributes=attributes,
                )
                prediction = self.inference.predict(transaction, features)
                result = FraudScoringResult(
                    **prediction.model_dump(), transaction_id=request.transaction_id,
                    historical_features=metadata,
                    risk_indicators=observed_risk_indicators(request.amount, features),
                )
            # Return after the guard exits, so release/verification errors fail too.
            return result
        except (FraudScoringError, ModelError):
            raise
        except Exception:
            raise FraudScoringError("Fraud scoring failed") from None
