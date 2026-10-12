"""Additive migration. Stop API/workers before upgrading an existing database."""

from sqlalchemy import inspect


def upgrade_graph_population(engine) -> bool:
    with engine.begin() as connection:
        inspector = inspect(connection)
        if "graph_sync_jobs" not in inspector.get_table_names():
            raise RuntimeError("Initialize tables with python -m scripts.init_db first")
        if "graph_population_id" in {c["name"] for c in inspector.get_columns("graph_sync_jobs")}:
            return False
        connection.exec_driver_sql("ALTER TABLE graph_sync_jobs ADD COLUMN graph_population_id VARCHAR(64)")
    return True


if __name__ == "__main__":
    from app.core.database import engine
    changed = upgrade_graph_population(engine)
    print("Added graph population column." if changed else "Population column already present; no changes.")
