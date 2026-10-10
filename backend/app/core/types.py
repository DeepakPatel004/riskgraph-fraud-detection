from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """Store UTC instants and always return timezone-aware UTC datetimes."""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        # SQLite stores UTC without an offset; PostgreSQL uses TIMESTAMP WITH TIME ZONE.
        return dialect.type_descriptor(DateTime(timezone=dialect.name != "sqlite"))

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Timestamp must include a timezone")
        value = value.astimezone(timezone.utc)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        # Legacy naive database values are assumed to have been intended as UTC.
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
