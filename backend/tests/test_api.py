from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app.core.database import Base, get_db
from app.main import app as api_app
from app.models import Transaction  # registers models


def test_root():
    with TestClient(api_app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert response.json()["message"] == "RiskGraph API"


def test_health_with_temporary_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)

    def override_db():
        with Session(engine) as session:
            yield session

    api_app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(api_app) as client:
            response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "database": "connected"}
    finally:
        api_app.dependency_overrides.clear()
        engine.dispose()
