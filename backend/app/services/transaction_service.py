import hashlib
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from fastapi import HTTPException
from app.models import GraphSyncJob, IdempotencyRecord, Transaction
from app.schemas.transaction import TransactionCreate


def create_transaction(db: Session, data: TransactionCreate, key: str | None) -> tuple[Transaction, bool]:
    # The hash is based on validated values, so whitespace/key ordering cannot change it.
    request_hash = hashlib.sha256(data.model_dump_json().encode("utf-8")).hexdigest()

    if key is not None:
        prior = db.get(IdempotencyRecord, key)
        if prior:
            if prior.request_hash != request_hash:
                raise HTTPException(status_code=409, detail="Idempotency key was used for a different request")
            existing = db.get(Transaction, prior.transaction_id)
            if existing is None:
                raise HTTPException(status_code=500, detail="Idempotency record is inconsistent")
            return existing, False

    if db.get(Transaction, data.transaction_id):
        raise HTTPException(status_code=409, detail="Transaction ID already exists")

    # Once scoring has begun, late legacy writes would retroactively change the
    # graph history used by saved predictions. Idempotent replays above are exempt.
    latest_score = db.scalar(select(func.max(Transaction.event_time)).where(Transaction.risk_score.is_not(None)))
    if latest_score is not None and data.event_time < latest_score:
        raise HTTPException(status_code=409, detail="Late event precedes an accepted scored transaction")

    transaction = Transaction(**data.model_dump(), processing_status="RECEIVED", graph_sync_status="PENDING")
    db.add(transaction)
    try:
        # Insert the parent first, without committing either record yet.
        db.flush()
        # The outbox job commits with its parent and any idempotency record.
        db.add(GraphSyncJob(transaction_id=data.transaction_id))
        if key is not None:
            db.add(IdempotencyRecord(idempotency_key=key, request_hash=request_hash,
                                     transaction_id=data.transaction_id, status="COMPLETED"))
        db.commit()
    except IntegrityError:
        db.rollback()
        # A concurrent request may have committed after our initial checks.
        if key is not None:
            prior = db.get(IdempotencyRecord, key)
            if prior:
                if prior.request_hash != request_hash:
                    raise HTTPException(status_code=409, detail="Idempotency key was used for a different request")
                existing = db.get(Transaction, prior.transaction_id)
                if existing is None:
                    raise HTTPException(status_code=500, detail="Idempotency record is inconsistent")
                return existing, False
        if db.get(Transaction, data.transaction_id):
            raise HTTPException(status_code=409, detail="Transaction ID already exists")
        # Let unexpected integrity failures become server errors, not conflicts.
        raise
    db.refresh(transaction)
    return transaction, True


def get_transaction(db: Session, transaction_id: str) -> Transaction:
    transaction = db.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return transaction


def list_transactions(db: Session, limit: int, offset: int):
    total = db.scalar(select(func.count()).select_from(Transaction)) or 0
    rows = db.scalars(select(Transaction).order_by(Transaction.event_time.desc(), Transaction.transaction_id.desc())
                      .limit(limit).offset(offset)).all()
    return {"items": rows, "total": total, "limit": limit, "offset": offset}
