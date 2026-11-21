"""Run pending jobs in the API process while holding durable, expiring leases."""

import logging
import sqlite3
import threading

from .backend import Backend, EngineError
from .store import Lease, Store

_LOG = logging.getLogger(__name__)
_POLL_SECONDS = 0.5


class Worker:
    def __init__(self, store: Store, backend: Backend):
        self._store = store
        self._backend = backend
        self._stopping = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.alive:
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="proof-worker")
        self._thread.start()

    def notify(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stopping.set()
        self.notify()
        if self._thread is not None:
            self._thread.join()

    def _run(self) -> None:
        # Each job has three separately bounded subprocesses. Allow extra time to
        # commit results and avoid handing off a lease during normal execution.
        lease_seconds = 3 * self._backend.timeout + 30
        while not self._stopping.is_set():
            self._wake.clear()
            try:
                lease = self._store.claim(lease_seconds)
                if lease is not None:
                    self._execute(lease)
                    continue
            except sqlite3.Error:
                _LOG.error("proof worker could not update the job database")
            self._wake.wait(_POLL_SECONDS)

    def _execute(self, lease: Lease) -> None:
        try:
            result = self._backend.prove(lease.program)
        except EngineError as exc:
            self._store.fail(lease, exc.code)
            _LOG.warning("proof job %s failed (%s)", lease.job_id, exc.code)
        except Exception as exc:
            self._store.fail(lease, "engine_internal")
            _LOG.error("proof job %s failed (%s)", lease.job_id, type(exc).__name__)
        else:
            if not self._store.succeed(lease, result):
                _LOG.warning("proof job %s result discarded after lease expiry", lease.job_id)
