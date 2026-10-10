"""CLI outbox worker. Memory mode is explicit and requires an ephemeral-data warning."""

import argparse
import logging
import math
import signal
from threading import Event

from app.services.graph_sync_service import (
    GraphSyncOptions, GraphSyncPersistenceError, GraphSyncService, retry_failed_job,
)


logger = logging.getLogger(__name__)


def run_worker(service: GraphSyncService, *, once: bool = False, batch_size: int = 20,
               poll_interval: float = 1, stop_event: Event | None = None) -> int:
    """Finish the active job on shutdown; leave interrupted work to lease recovery."""
    if not math.isfinite(poll_interval) or poll_interval <= 0:
        raise ValueError("poll_interval must be positive and finite")
    stop = stop_event if stop_event is not None else Event()
    while not stop.is_set():
        try:
            count = service.process_batch(batch_size, should_stop=stop.is_set)
            logger.info("Graph sync batch handled %d jobs", count)
        except GraphSyncPersistenceError as error:
            logger.error("%s", error)  # Error text is fixed by our service, not a database exception.
            if once:
                return 1
        if once:
            return 0
        stop.wait(poll_interval)
    return 0


def main(argv=None) -> int:
    from app.core.config import get_settings
    from app.core.database import SessionLocal
    from sqlalchemy.exc import SQLAlchemyError

    settings = get_settings()
    parser = argparse.ArgumentParser(description="Process RiskGraph graph-sync outbox jobs")
    parser.add_argument("--once", action="store_true", help="Process one batch and exit")
    parser.add_argument("--adapter", choices=["disabled", "memory", "neptune"], default=settings.graph_sync_adapter)
    parser.add_argument("--allow-ephemeral", action="store_true", help="Acknowledge memory graph data loss on restart")
    parser.add_argument("--batch-size", type=int, default=settings.graph_sync_batch_size)
    parser.add_argument("--poll-interval", type=float, default=settings.graph_sync_poll_interval)
    parser.add_argument("--retry-failed", metavar="JOB_ID", help="Reset a terminal failure and exit without graph access")
    args = parser.parse_args(argv)
    if args.batch_size < 1 or not math.isfinite(args.poll_interval) or args.poll_interval <= 0:
        parser.error("batch size and polling interval must be positive and finite")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.retry_failed:
        try:
            reset = retry_failed_job(SessionLocal, args.retry_failed)
        except SQLAlchemyError:
            logger.error("Could not persist manual graph sync retry")
            return 1
        logger.info("Failed job reset" if reset else "No matching terminal failed job")
        return 0 if reset else 1
    if args.adapter == "disabled":
        parser.error("Choose an adapter explicitly; graph synchronization is disabled by default")
    client = None
    if args.adapter == "memory":
        if not args.allow_ephemeral:
            parser.error("Memory mode requires --allow-ephemeral; SYNCED graph data is lost on restart")
        from app.graph.in_memory import InMemoryGraphRepository
        graph = InMemoryGraphRepository()
        logger.warning("EPHEMERAL GRAPH: restart loses nodes; SYNCED jobs are not automatically replayed")
    else:
        from app.graph.neptune import NeptuneGraphRepository
        from app.graph.neptune_client import NeptuneClient
        try:
            client = NeptuneClient.from_settings(settings)
        except ValueError:
            parser.error("Neptune configuration is missing or invalid")
        graph = NeptuneGraphRepository(client)
    options = GraphSyncOptions(
        initial_delay=settings.graph_sync_initial_delay, backoff_factor=settings.graph_sync_backoff_factor,
        max_delay=settings.graph_sync_max_delay, max_attempts=settings.graph_sync_max_attempts,
        lease_seconds=settings.graph_sync_lease_seconds,
    )
    service = GraphSyncService(SessionLocal, graph, options)
    stop = Event()
    previous_handlers = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, lambda *_: stop.set())
        return run_worker(service, once=args.once, batch_size=args.batch_size,
                          poll_interval=args.poll_interval, stop_event=stop)
    except KeyboardInterrupt:
        return 0
    finally:
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
