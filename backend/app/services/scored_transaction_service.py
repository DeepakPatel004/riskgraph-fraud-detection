"""Atomic scored submissions. Network/model work occurs between SQL transactions."""

import hashlib

from fastapi import HTTPException
from app.api.scoring_errors import ScoringAPIError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.models import FraudAlert, GraphSyncJob, IdempotencyRecord, Transaction
from app.schemas.fraud_scoring import FraudScoringResponse
from app.services.risk_indicators import RISK_INDICATOR_VERSION


def saved_score(transaction) -> FraudScoringResponse:
    try:
        saved = transaction.features_json["fraud_scoring"]
        return FraudScoringResponse(
            transaction_id=transaction.transaction_id, risk_score=transaction.risk_score,
            risk_level=transaction.risk_level, model_version=saved["model_version"],
            graph_sync_status=transaction.graph_sync_status,
            risk_indicators=saved.get("risk_indicators", []),
        )
    except Exception:
        raise ScoringAPIError(500, "Saved scoring metadata is inconsistent", code="INCONSISTENT_STATE") from None


def resolve_existing(db, payload, key, request_hash):
    if key is not None:
        prior = db.get(IdempotencyRecord, key)
        if prior is not None:
            if prior.request_hash != request_hash:
                raise ScoringAPIError(409, "Idempotency key was used for a different request", code="IDEMPOTENCY_CONFLICT")
            transaction = db.get(Transaction, prior.transaction_id)
            if transaction is None or prior.status != "COMPLETED":
                raise ScoringAPIError(500, "Idempotency record is inconsistent", code="INCONSISTENT_STATE")
            return saved_score(transaction)
    if db.get(Transaction, payload.transaction_id) is not None:
        raise ScoringAPIError(409, "Transaction ID already exists", code="DUPLICATE_TRANSACTION")
    return None


def create_scored_transaction(db, payload, key, gate, scoring_factory):
    """The gate covers replay/order checks, inference and one atomic SQL commit."""
    request_hash = hashlib.sha256(
        ("fraud-score:v1:" + payload.model_dump_json()).encode(),
    ).hexdigest()
    try:
        with gate.hold():
            replay = resolve_existing(db, payload, key, request_hash)
            if replay is not None:
                return replay, False
            latest = db.scalar(select(func.max(Transaction.event_time)))
            if latest is not None and payload.event_time < latest:
                raise ScoringAPIError(409, "Late event precedes an accepted transaction", code="LATE_EVENT")
            engine = db.get_bind()
            # End the read transaction before constructing/calling graph dependencies.
            db.rollback()
            result = run_scoring(scoring_factory, engine, gate, payload)
            transaction = Transaction(
                transaction_id=payload.transaction_id, amount=payload.amount,
                currency=payload.currency, card_token=payload.card_token,
                device_token=payload.device_token, event_time=payload.event_time,
                processing_status="SCORED", graph_sync_status="PENDING",
                risk_score=result.risk_score, risk_level=result.risk_level,
                features_json={"fraud_scoring": {
                    "version": 1, "model_version": result.model_version,
                    "attributes": {name: getattr(payload, name) for name in
                                   ("ProductCD", "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9")},
                    "historical_features": result.historical_features.model_dump(mode="json"),
                    "risk_indicators": result.risk_indicators,
                    "risk_indicator_version": RISK_INDICATOR_VERSION,
                }},
            )
            db.add(transaction)
            try:
                db.flush()  # Parent first, with no separate commit.
                if transaction.risk_level == "HIGH":
                    db.add(FraudAlert(
                        transaction_id=transaction.transaction_id,
                        risk_score=transaction.risk_score,
                        risk_indicators=list(result.risk_indicators),
                        investigation_status="OPEN",
                    ))
                db.add(GraphSyncJob(transaction_id=payload.transaction_id))
                if key is not None:
                    db.add(IdempotencyRecord(
                        idempotency_key=key, transaction_id=payload.transaction_id,
                        request_hash=request_hash, status="COMPLETED",
                    ))
                db.commit()
            except IntegrityError:
                db.rollback()
                replay = resolve_existing(db, payload, key, request_hash)
                if replay is not None:
                    return replay, False
                raise  # Only verified conflicts above are 409.
            return saved_score(transaction), True
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise ScoringAPIError(500, "Could not persist scored transaction", code="PERSISTENCE_ERROR") from None
    except Exception as error:
        from app.services.ingestion_gate import IngestionBusy
        db.rollback()
        if isinstance(error, IngestionBusy):
            raise ScoringAPIError(503, "Transaction ingestion is busy; retry later",
                                code="INGESTION_BUSY", retryable=True,
                                headers={"Retry-After": "1"}) from None
        raise ScoringAPIError(500, "Scored transaction failed", code="SCORING_FAILED") from None


def run_scoring(factory, engine, gate, payload):
    try:
        # Lazy imports: health/read/legacy endpoints need no ML runtime or artifacts.
        from app.ml.fraud_predictor import ModelError
        from app.services.fraud_scoring_service import (
            FraudScoringError, GraphFreshnessError, GraphHistoryError,
        )
    except ImportError:
        raise ScoringAPIError(503, "Fraud model runtime is unavailable", code="MODEL_UNAVAILABLE", retryable=True) from None
    try:
        return factory(engine, gate).score(payload)
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise  # SQL checker failures remain server/database errors.
    except GraphFreshnessError:
        raise ScoringAPIError(503, "Historical graph is unavailable or incomplete",
                              code="GRAPH_FRESHNESS_UNAVAILABLE", retryable=True) from None
    except GraphHistoryError:
        raise ScoringAPIError(503, "Historical graph is unavailable or incomplete",
                              code="GRAPH_UNAVAILABLE", retryable=True) from None
    except ModelError:
        raise ScoringAPIError(503, "Fraud model is unavailable", code="MODEL_UNAVAILABLE", retryable=True) from None
    except FraudScoringError:
        raise ScoringAPIError(500, "Fraud scoring failed", code="SCORING_FAILED") from None
    except Exception:
        raise ScoringAPIError(500, "Fraud scoring failed", code="SCORING_FAILED") from None
