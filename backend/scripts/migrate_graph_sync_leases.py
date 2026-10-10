"""Additive, repeatable migration; stop API/workers while upgrading SQLite."""

from sqlalchemy import inspect


def upgrade_graph_sync_leases(engine) -> bool:
    """Keep every row/table/index; add only a nullable claim-ownership column."""
    with engine.begin() as connection:
        inspector = inspect(connection)
        if "graph_sync_jobs" not in inspector.get_table_names():
            raise RuntimeError("Initialize tables with python -m scripts.init_db first")
        if "lease_token" in {column["name"] for column in inspector.get_columns("graph_sync_jobs")}:
            return False
        connection.exec_driver_sql("ALTER TABLE graph_sync_jobs ADD COLUMN lease_token VARCHAR(36)")
    return True


if __name__ == "__main__":
    from app.core.database import engine
    changed = upgrade_graph_sync_leases(engine)
    print("Added graph_sync_jobs.lease_token." if changed else "Lease column already present; no changes.")
