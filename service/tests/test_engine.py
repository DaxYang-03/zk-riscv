import hashlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from zkservice.backend import Backend, EngineError, InvalidProgram, ProofResult, normalize_program
from zkservice.store import Busy, Conflict, Store
from zkservice.worker import Worker

PROGRAM = (0x00700293, 0xFFD28293, 0xFFF00313, 0x00230313)
OTHER_PROGRAM = (0x00800293, *PROGRAM[1:])
RESULT = ProofResult(b"proof", 4, 1)


class ProgramTests(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(normalize_program(("00700293", "0XFFD28293", *PROGRAM[2:])), PROGRAM)

    def test_rejects_unsupported_programs(self):
        for words in (
            PROGRAM[1:],
            (*PROGRAM, 0x13),
            ("0700293", *PROGRAM[1:]),
            (True, *PROGRAM[1:]),
            (0x1_00700293, *PROGRAM[1:]),
            (PROGRAM[0] | 0x1000, *PROGRAM[1:]),
            (0xFFFF_FFFF, *PROGRAM[1:]),
        ):
            with self.subTest(words=words), self.assertRaises(InvalidProgram):
                normalize_program(words)


class BackendTests(unittest.TestCase):
    def test_operational_secrets_are_not_forwarded_to_the_verifier(self):
        backend = Backend(sys.executable, __file__, sys.executable)
        verified = subprocess.CompletedProcess([], 0, stdout=b"verification=ok\n")
        with patch.dict("os.environ", {"ZKR_API_TOKEN": "secret"}):
            with patch("zkservice.backend.subprocess.run", return_value=verified) as run:
                self.assertTrue(backend.verify(PROGRAM, 4, 1, b"proof"))
        self.assertNotIn("ZKR_API_TOKEN", run.call_args.kwargs["env"])
        self.assertIn("PATH", run.call_args.kwargs["env"])

    def test_timeout_returns_a_stable_error_code(self):
        backend = Backend(sys.executable, __file__, sys.executable, timeout=0.001)
        with patch("zkservice.backend.subprocess.run", side_effect=subprocess.TimeoutExpired([], 1)):
            with self.assertRaises(EngineError) as timeout:
                backend.verify(PROGRAM, 4, 1, b"proof")
        self.assertEqual(timeout.exception.code, "engine_timeout")

    def test_empty_and_oversized_proofs_are_not_executed(self):
        backend = Backend(sys.executable, __file__, sys.executable)
        with patch("zkservice.backend.subprocess.run") as run:
            self.assertFalse(backend.verify(PROGRAM, 4, 1, b""))
            self.assertFalse(backend.verify(PROGRAM, 4, 1, bytes(65_537)))
        run.assert_not_called()


class StoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "jobs.db"

    def test_concurrent_idempotent_requests_share_a_job(self):
        store = Store(self.path)
        with ThreadPoolExecutor(max_workers=5) as executor:
            receipts = list(executor.map(lambda _: store.submit(PROGRAM, "key"), range(10)))
        self.assertEqual(sum(created for _, created in receipts), 1)
        self.assertEqual(len({job.id for job, _ in receipts}), 1)
        with self.assertRaises(Conflict):
            store.submit(OTHER_PROGRAM, "key")

    def test_queue_and_storage_caps_preserve_existing_receipts(self):
        store = Store(self.path, max_live_jobs=1, max_stored_jobs=1)
        job, _ = store.submit(PROGRAM, "first")
        with self.assertRaisesRegex(Busy, "queue_full"):
            store.submit(PROGRAM, "second")
        lease = store.claim(60)
        self.assertTrue(store.succeed(lease, RESULT))
        with self.assertRaisesRegex(Busy, "storage_full"):
            store.submit(PROGRAM, "second")
        receipt, created = store.submit(PROGRAM, "first")
        self.assertFalse(created)
        self.assertEqual(receipt.id, job.id)

    def test_artifact_and_metadata_survive_new_store_instance(self):
        store = Store(self.path)
        job, _ = store.submit(PROGRAM)
        lease = store.claim(60)
        self.assertIsNone(store.artifact(job.id))
        self.assertTrue(store.succeed(lease, RESULT))
        reopened = Store(self.path)
        reopened.ping()
        after = reopened.get(job.id)
        self.assertEqual(after.status, "succeeded")
        self.assertEqual(after.attempts, 1)
        self.assertEqual((after.x5, after.x6), (4, 1))
        self.assertEqual(after.proof_sha256, hashlib.sha256(RESULT.proof).hexdigest())
        self.assertEqual(after.proof_size, len(RESULT.proof))
        self.assertEqual(reopened.artifact(job.id), RESULT.proof)

    def test_expiry_recovers_and_rejects_stale_writes(self):
        store = Store(self.path, max_attempts=2)
        job, _ = store.submit(PROGRAM)
        with patch("zkservice.store.time.time", return_value=1000):
            first = store.claim(60)
        with patch("zkservice.store.time.time", return_value=1061):
            self.assertFalse(store.succeed(first, RESULT))
            second = Store(self.path, max_attempts=2).claim(60)
            self.assertFalse(store.fail(first, "invalid"))
        self.assertNotEqual(first.token, second.token)
        self.assertEqual(first.job_id, second.job_id)
        with patch("zkservice.store.time.time", return_value=1122):
            self.assertIsNone(store.claim(60))
        after = store.get(job.id)
        self.assertEqual((after.status, after.error_code, after.attempts), (
            "failed", "attempts_exhausted", 2
        ))
        self.assertIsNone(store.artifact(job.id))

    def test_pruning_only_matches_terminal_jobs_and_defaults_to_dry_run(self):
        store = Store(self.path)
        old_job, _ = store.submit(PROGRAM, "old-key")
        lease = store.claim(60)
        self.assertTrue(store.succeed(lease, RESULT))
        active_job, _ = store.submit(PROGRAM, "active-key")
        cutoff = time.time() - 86_400
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE jobs SET updated_at = ?", (cutoff - 60,))
        self.assertEqual(Store(self.path, mode="ro").prune_terminal(cutoff), 1)
        self.assertEqual(store.artifact(old_job.id), RESULT.proof)
        self.assertEqual(Store(self.path, mode="rw").prune_terminal(cutoff, apply=True), 1)
        self.assertIsNone(store.get(old_job.id))
        self.assertIsNone(store.artifact(old_job.id))
        self.assertEqual(store.get(active_job.id).status, "queued")
        _, created = store.submit(PROGRAM, "old-key")
        self.assertTrue(created)

    def test_unrelated_database_is_not_modified(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("CREATE TABLE other (id INTEGER)")
        original = self.path.read_bytes()
        for mode in ("ro", "rw", "rwc"):
            with self.assertRaises(sqlite3.OperationalError):
                Store(self.path, mode=mode)
            self.assertEqual(self.path.read_bytes(), original)

    def test_worker_persists_failures_and_keeps_running(self):
        class FailingEngine:
            timeout = 1.0

            def prove(self, program):
                if program == PROGRAM:
                    raise EngineError("engine_timeout")
                return RESULT

        store = Store(self.path)
        first, _ = store.submit(PROGRAM)
        second, _ = store.submit(OTHER_PROGRAM)
        worker = Worker(store, FailingEngine())
        worker.start()
        self.addCleanup(worker.stop)
        worker.notify()
        deadline = time.monotonic() + 3
        while (
            store.get(second.id).status != "succeeded"
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        self.assertTrue(worker.alive)
        self.assertEqual(store.get(first.id).error_code, "engine_timeout")
        self.assertEqual(store.get(second.id).status, "succeeded")


if __name__ == "__main__":
    unittest.main()
