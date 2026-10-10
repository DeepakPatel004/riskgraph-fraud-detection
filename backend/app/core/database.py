from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str, *, poolclass=None):
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    db_engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True, poolclass=poolclass)
    if db_engine.dialect.name == "sqlite":
        @event.listens_for(db_engine, "connect")
        def enable_foreign_keys(connection, connection_record):
            cursor = connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()
    return db_engine


engine = make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    with SessionLocal() as session:
        yield session
