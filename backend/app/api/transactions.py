from typing import Annotated
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.schemas.transaction import TransactionCreate, TransactionRead, TransactionPage
from app.services.transaction_service import create_transaction, get_transaction, list_transactions
from app.api.scoring_dependencies import get_scoring_factory
from app.schemas.fraud_scoring import FraudScoringRequest, FraudScoringResponse
from app.services.ingestion_gate import IngestionBusy, ingestion_gate
from app.services.scored_transaction_service import create_scored_transaction

router = APIRouter(prefix="/api/v1/transactions", tags=["transactions"])


@router.post("", response_model=TransactionRead, status_code=201)
def create(payload: TransactionCreate, response: Response, db: Session = Depends(get_db),
           idempotency_key: Annotated[str | None, Header(min_length=1, max_length=128)] = None):
    try:
        with ingestion_gate(db.get_bind()).hold():
            transaction, created = create_transaction(db, payload, idempotency_key)
    except IngestionBusy:
        raise HTTPException(503, "Transaction ingestion is busy; retry later",
                            headers={"Retry-After": "1"}) from None
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(500, "Could not persist transaction") from None
    if not created:
        response.status_code = 200
    return transaction


@router.post("/score", response_model=FraudScoringResponse, status_code=201,
             summary="Score and save a USD transaction",
             description="Uses strictly earlier, verified graph history and the saved model. "
                         "Atomically saves the prediction and one graph outbox job. "
                         "Identical Idempotency-Key retries return 200 without inference. "
                         "New transactions return 201; unsafe history or unavailable model returns 503.",
             responses={200: {"description": "Previously committed prediction"},
                        409: {"description": "Conflicting key/ID or late event"},
                        503: {"description": "Busy ingestion, unknown history, or model unavailable"},
                        500: {"description": "Unexpected persistence/server failure"}})
def score(payload: FraudScoringRequest, response: Response, db: Session = Depends(get_db),
          factory=Depends(get_scoring_factory),
          idempotency_key: Annotated[str | None, Header(min_length=1, max_length=128)] = None):
    result, created = create_scored_transaction(
        db, payload, idempotency_key, ingestion_gate(db.get_bind()), factory,
    )
    if not created:
        response.status_code = 200
    return result


@router.get("", response_model=TransactionPage)
def all_transactions(limit: int = Query(default=20, ge=1, le=100),
                     offset: int = Query(default=0, ge=0), db: Session = Depends(get_db)):
    return list_transactions(db, limit, offset)


@router.get("/{transaction_id}", response_model=TransactionRead)
def by_id(transaction_id: str, db: Session = Depends(get_db)):
    return get_transaction(db, transaction_id)
