from app.core.database import Base, engine
import app.models  # register mapped models with SQLAlchemy


if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    print("Database tables created.")
