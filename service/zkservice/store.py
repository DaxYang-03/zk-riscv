"""Durable SQLite job queue and atomically committed proof artifacts."""

import hashlib
import json
import math
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .backend import ProofResult, normalize_program


class Busy(RuntimeError):
    """The operator's queue or storage limit has been reached."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Conflict(RuntimeError):
    """An idempotency key was previously used with a different program."""


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    program: tuple[int, ...]
    status: str
    created_at: str
    updated_at: str
    attempts: int
    x5: int | None
    x6: int | None
    proof_sha256: str | None
    proof_size: int | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class Lease:
    job_id: str
    token: str
    program: tuple[int, ...]


_APPLICATION_ID = int.from_bytes(b"ZKRV", "big")
_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT NOT NULL PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    program TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease_token TEXT,
    lease_until REAL,
    x5 INTEGER,
    x6 INTEGER,
    proof BLOB,
    proof_sha256 TEXT,
    error_code TEXT,
    CHECK ((status = 'running') = (lease_token IS NOT NULL AND lease_until IS NOT NULL)),
    CHECK ((status = 'succeeded') = (x5 IS NOT NULL AND x6 IS NOT NULL
        AND proof IS NOT NULL AND proof_sha256 IS NOT NULL)),
    CHECK ((status = 'failed') = (error_code IS NOT NULL))
)
"""
_METADATA = """
SELECT id, program, status, created_at, updated_at, attempts,
       x5, x6, proof_sha256, length(proof) AS proof_size, error_code
FROM jobs
"""


def _timestamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _job(row: sqlite3.Row) -> Job:
    values = dict(row)
    values["program"] = tuple(json.loads(values["program"]))
    values["created_at"] = _timestamp(values["created_at"])
    values["updated_at"] = _timestamp(values["updated_at"])
    return Job(**values)


class Store:
    """A queue for one API process, with leases to guard against stale writes."""

    def __init__(
        self,
        path: str | Path,
        max_live_jobs: int = 128,
        max_stored_jobs: int = 10_000,
        max_attempts: int = 3,
        *,
        mode: Literal["rwc", "rw", "ro"] = "rwc",
    ):
        if not 0 < max_live_jobs <= max_stored_jobs or max_attempts < 1:
            raise ValueError("invalid queue limits")
        if mode not in ("rwc", "rw", "ro"):
            raise ValueError("invalid database mode")
        self.path = Path(path).expanduser().resolve()
        self._database = self.path.as_uri() + f"?mode={mode}"
        self.max_live_jobs = max_live_jobs
        self.max_stored_jobs = max_stored_jobs
        self.max_attempts = max_attempts

        if mode == "rwc":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            if mode != "rwc":
                self._check_schema(connection)
                return
            self._initialize(connection)
            if connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] != "wal":
                raise sqlite3.OperationalError("could not enable WAL on proof database")

    def _initialize(self, connection: sqlite3.Connection) -> None:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            application = connection.execute("PRAGMA application_id").fetchone()[0]
            existing_objects = connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
            if application != 0 or version != 0 or existing_objects is not None:
                self._check_schema(connection)
                return
            connection.execute(_SCHEMA)
            connection.execute("CREATE INDEX jobs_queue ON jobs(status, created_at, id)")
            connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version = {_VERSION}")

    @staticmethod
    def _check_schema(connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        application = connection.execute("PRAGMA application_id").fetchone()[0]
        if application != _APPLICATION_ID or version != _VERSION:
            raise sqlite3.OperationalError("unsupported proof database schema")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=5.0, uri=True)
        try:
            connection.execute("PRAGMA synchronous = FULL")
        except sqlite3.Error:
            connection.close()
            raise
        connection.row_factory = sqlite3.Row
        return connection

    def ping(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("SELECT 1 FROM jobs LIMIT 1").fetchone()

    def submit(
        self, program: tuple[int, ...], idempotency_key: str | None = None
    ) -> tuple[Job, bool]:
        program = normalize_program(program)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key is not None:
                row = connection.execute(
                    _METADATA + " WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if row is not None:
                    job = _job(row)
                    if job.program != program:
                        raise Conflict("idempotency key already names a different program")
                    return job, False
            counts = connection.execute(
                "SELECT count(*), count(CASE WHEN status IN ('queued', 'running') THEN 1 END) "
                "FROM jobs"
            ).fetchone()
            if counts[1] >= self.max_live_jobs:
                raise Busy("queue_full")
            if counts[0] >= self.max_stored_jobs:
                raise Busy("storage_full")
            job_id = str(uuid.uuid4())
            now = time.time()
            connection.execute(
                "INSERT INTO jobs (id, idempotency_key, program, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'queued', ?, ?)",
                (job_id, idempotency_key, json.dumps(program), now, now),
            )
            return _job(connection.execute(_METADATA + " WHERE id = ?", (job_id,)).fetchone()), True

    def get(self, job_id: str) -> Job | None:
        with closing(self._connect()) as connection:
            row = connection.execute(_METADATA + " WHERE id = ?", (job_id,)).fetchone()
            return _job(row) if row is not None else None

    def artifact(self, job_id: str) -> bytes | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT proof FROM jobs WHERE id = ? AND status = 'succeeded'", (job_id,)
            ).fetchone()
            return row[0] if row is not None else None

    def prune_terminal(self, before: float, *, apply: bool = False) -> int:
        if not math.isfinite(before) or before >= time.time():
            raise ValueError("retention cutoff must be in the past")
        criteria = " FROM jobs WHERE status IN ('succeeded', 'failed') AND updated_at < ?"
        with closing(self._connect()) as connection, connection:
            if apply:
                deleted = connection.execute("DELETE" + criteria, (before,))
                return deleted.rowcount
            return connection.execute("SELECT count(*)" + criteria, (before,)).fetchone()[0]

    def claim(self, lease_seconds: float) -> Lease | None:
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("invalid lease duration")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            now = time.time()
            connection.execute(
                """
                UPDATE jobs
                SET status = CASE WHEN attempts >= ? THEN 'failed' ELSE 'queued' END,
                    error_code = CASE WHEN attempts >= ? THEN 'attempts_exhausted' END,
                    lease_token = NULL, lease_until = NULL, updated_at = ?
                WHERE status = 'running' AND lease_until <= ?
                """,
                (self.max_attempts, self.max_attempts, now, now),
            )
            row = connection.execute(
                "SELECT id, program FROM jobs WHERE status = 'queued' "
                "ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            connection.execute(
                """
                UPDATE jobs SET status = 'running', lease_token = ?, lease_until = ?,
                    updated_at = ?, attempts = attempts + 1
                WHERE id = ?
                """,
                (token, now + lease_seconds, now, row["id"]),
            )
            return Lease(row["id"], token, tuple(json.loads(row["program"])))

    def succeed(self, lease: Lease, result: ProofResult) -> bool:
        digest = hashlib.sha256(result.proof).hexdigest()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            now = time.time()
            update = connection.execute(
                """
                UPDATE jobs SET status = 'succeeded', lease_token = NULL, lease_until = NULL,
                    x5 = ?, x6 = ?, proof = ?, proof_sha256 = ?, updated_at = ?
                WHERE id = ? AND lease_token = ? AND lease_until > ?
                """,
                (result.x5, result.x6, result.proof, digest, now, lease.job_id, lease.token, now),
            )
            return update.rowcount == 1

    def fail(self, lease: Lease, error_code: str) -> bool:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            now = time.time()
            update = connection.execute(
                """
                UPDATE jobs SET status = 'failed', lease_token = NULL, lease_until = NULL,
                    error_code = ?, updated_at = ?
                WHERE id = ? AND lease_token = ? AND lease_until > ?
                """,
                (error_code, now, lease.job_id, lease.token, now),
            )
            return update.rowcount == 1
