"""Single-host ingestion serialization without holding a SQL transaction open."""

import os
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, local
from weakref import WeakKeyDictionary


class IngestionBusy(RuntimeError):
    pass


class IngestionGate:
    """Thread exclusion plus an OS file lock shared by cooperating API processes.

    File locks release on process exit. This does not coordinate other hosts,
    direct SQL writers, or shared/network filesystems. A busy caller retries.
    """

    def __init__(self, path: Path | None = None):
        self.path = path
        self._lock = Lock()
        self._state = local()

    @property
    def held(self):
        return getattr(self._state, "held", False)

    @contextmanager
    def hold(self):
        if not self._lock.acquire(blocking=False):
            raise IngestionBusy("Transaction ingestion is busy; retry later")
        handle = None
        locked = False
        try:
            if self.path is not None:
                handle = self.path.open("a+b")
                if handle.seek(0, 2) == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except OSError:
                    raise IngestionBusy("Transaction ingestion is busy; retry later") from None
            self._state.held = True
            yield
        finally:
            self._state.held = False
            try:
                if handle is not None:
                    if locked:
                        handle.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                if handle is not None:
                    handle.close()
                self._lock.release()


_gates = WeakKeyDictionary()
_registry_lock = Lock()


def sqlite_source_path(engine) -> Path | None:
    if engine.dialect.name != "sqlite":
        return None
    database = engine.url.database
    if (not database or database == ":memory:" or database.startswith("file:")
            or engine.url.query.get("uri") == "true"):
        return None
    return Path(database).resolve()


def ingestion_gate(engine) -> IngestionGate:
    with _registry_lock:
        if engine not in _gates:
            source = sqlite_source_path(engine)
            path = source.with_name(source.name + ".ingestion.lock") if source else None
            _gates[engine] = IngestionGate(path)
        return _gates[engine]
