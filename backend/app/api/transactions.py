from typing import Annotated
from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.schemas.transaction import TransactionCreate, TransactionRead, TransactionPage
from app.services.transaction_service import create_transaction, get_transaction, list_transactions

router = APIRouter(prefix="/api/v1/transactions", tags=["transactions"])


@router.post("", response_model=TransactionRead, status_code=201)
def create(payload: TransactionCreate, response: Response, db: Session = Depends(get_db),
           idempotency_key: Annotated[str | None, Header(min_length=1, max_length=128)] = None):
    transaction, created = create_transaction(db, payload, idempotency_key)
    if not created:
        response.status_code = 200
    return transaction


@router.get("", response_model=TransactionPage)
def all_transactions(limit: int = Query(default=20, ge=1, le=100),
                     offset: int = Query(default=0, ge=0), db: Session = Depends(get_db)):
    return list_transactions(db, limit, offset)


@router.get("/{transaction_id}", response_model=TransactionRead)
def by_id(transaction_id: str, db: Session = Depends(get_db)):
    return get_transaction(db, transaction_id)
