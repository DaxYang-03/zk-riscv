"""Exercise the HTTP protocol using the real queue and a controlled proof backend."""

import hashlib
import time
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from zkservice import app
from zkservice.backend import EngineError, ProofResult
from zkservice.config import Settings
from zkservice.schemas import CIRCUIT_ID

TOKEN = "test-key-only-unused-in-production"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
PROGRAM = ("0x00700293", "0xffd28293", "0xfff00313", "0x00230313")
OTHER_PROGRAM = ("0x00800293", *PROGRAM[1:])
PROOF = b"test proof artifact for HTTP protocol tests"


class FakeBackend:
    def __init__(self, *arguments):
        self.timeout = 0.1

    def validate(self):
        pass

    def prove(self, program):
        return ProofResult(PROOF, x5=4, x6=1)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "Backend", FakeBackend)
    return Settings(api_token=TOKEN, database=tmp_path / "state" / "jobs.sqlite3")


def wait_for_terminal(client, location):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(location, headers=HEADERS)
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in ("succeeded", "failed"):
            return job
        assert response.headers["Retry-After"] == "1"
        time.sleep(0.01)
    pytest.fail("proof job did not reach a terminal state")


def test_health_auth_and_discoverable_circuit(settings):
    with TestClient(app.create_app(settings)) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/readyz").json() == {"status": "ok"}
        denied = client.get("/v1/capabilities", headers={"Authorization": "Bearer invalid"})
        assert denied.status_code == 401
        assert denied.headers["WWW-Authenticate"] == "Bearer"
        assert denied.json()["error"]["code"] == "unauthorized"
        assert "invalid" not in denied.json()["error"]["message"].lower().split()

        response = client.get("/v1/capabilities", headers=HEADERS)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json()["circuit_id"] == CIRCUIT_ID
        assert response.json()["public_inputs"][-2:] == ["x5", "x6"]
        spec = client.get("/openapi.json").json()
        assert spec["paths"]["/v1/jobs"]["post"]["security"] == [{"OperatorBearer": []}]


def test_proof_export_idempotency_and_restart(settings):
    headers = {**HEADERS, "Idempotency-Key": "orders-41"}
    with TestClient(app.create_app(settings)) as client:
        submitted = client.post("/v1/jobs", headers=headers, json={"instructions": PROGRAM})
        assert submitted.status_code == 202, submitted.text
        location = submitted.headers["Location"]
        assert location == "/v1/jobs/" + submitted.json()["id"]
        assert submitted.headers["Retry-After"] == "1"
        job = wait_for_terminal(client, location)
        assert job["status"] == "succeeded"
        result = job["result"]
        assert (result["x5"], result["x6"]) == (4, 1)
        assert result["proof_sha256"] == hashlib.sha256(PROOF).hexdigest()
        assert result["proof_size"] == len(PROOF)
        assert result["circuit_id"] == CIRCUIT_ID

    with TestClient(app.create_app(settings)) as restarted:
        replay = restarted.post("/v1/jobs", headers=headers, json={"instructions": PROGRAM})
        assert replay.status_code == 200
        assert replay.json()["attempts"] == 1
        assert replay.json() == job

        proof = restarted.get(result["proof_url"], headers=HEADERS)
        assert proof.status_code == 200
        assert proof.content == PROOF
        assert proof.headers["Content-Type"] == "application/octet-stream"
        assert proof.headers["X-Proof-SHA256"] == result["proof_sha256"]
        assert proof.headers["X-ZKR-Circuit"] == CIRCUIT_ID

        conflict = restarted.post("/v1/jobs", headers=headers, json={"instructions": OTHER_PROGRAM})
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_rejects_invalid_and_oversized_requests(settings):
    with TestClient(app.create_app(settings)) as client:
        def post(**kwargs):
            return client.post("/v1/jobs", headers=HEADERS, **kwargs)

        invalid_shape = post(json={"instructions": PROGRAM[:-1]})
        assert invalid_shape.status_code == 422
        sensitive_input = "submitted text that must never echo"
        invalid_hex = post(json={"instructions": (sensitive_input, *PROGRAM[1:])})
        assert invalid_hex.json()["error"]["code"] == "invalid_request"
        assert sensitive_input not in invalid_hex.text

        illegal_opcode = post(json={"instructions": ("0x00000073", *PROGRAM[1:])})
        assert illegal_opcode.status_code == 422
        assert illegal_opcode.json()["error"]["code"] == "unsupported_program"

        for content in (b"x" * 4_097, iter((b"x" * 2_500, b"y" * 2_500))):
            oversized = post(content=content)
            assert oversized.status_code == 413
            assert oversized.json()["error"]["code"] == "request_too_large"

        missing = client.get(f"/v1/jobs/{uuid4()}", headers=HEADERS)
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "job_not_found"


def test_live_queue_capacity_and_pending_proof(settings, monkeypatch):
    finish = Event()

    class SlowBackend(FakeBackend):
        def prove(self, program):
            assert finish.wait(5), "test did not allow the worker to finish"
            return super().prove(program)

    monkeypatch.setattr(app, "Backend", SlowBackend)
    settings = Settings(api_token=TOKEN, database=settings.database, max_live_jobs=1, max_stored_jobs=1)
    with TestClient(app.create_app(settings)) as client:
        try:
            first = client.post("/v1/jobs", headers=HEADERS, json={"instructions": PROGRAM})
            assert first.status_code == 202
            not_ready = client.get(first.headers["Location"] + "/proof", headers=HEADERS)
            assert not_ready.status_code == 409
            assert not_ready.json()["error"]["code"] == "proof_unavailable"
            over_capacity = client.post("/v1/jobs", headers=HEADERS, json={"instructions": OTHER_PROGRAM})
            assert over_capacity.status_code == 429
            assert over_capacity.headers["Retry-After"] == "1"
            assert over_capacity.json()["error"]["code"] == "queue_full"
        finally:
            finish.set()
        assert wait_for_terminal(client, first.headers["Location"])["status"] == "succeeded"
        storage_full = client.post("/v1/jobs", headers=HEADERS, json={"instructions": OTHER_PROGRAM})
        assert storage_full.status_code == 429
        assert storage_full.json()["error"]["code"] == "storage_full"
        assert "Retry-After" not in storage_full.headers


def test_engine_errors_have_public_codes(settings, monkeypatch):
    class BrokenBackend(FakeBackend):
        def prove(self, program):
            raise EngineError("engine_timeout")

    monkeypatch.setattr(app, "Backend", BrokenBackend)
    with TestClient(app.create_app(settings)) as client:
        submitted = client.post("/v1/jobs", headers=HEADERS, json={"instructions": PROGRAM})
        failed = wait_for_terminal(client, submitted.headers["Location"])
        assert failed["status"] == "failed"
        assert failed["result"] is None
        assert failed["error"]["code"] == "engine_timeout"
