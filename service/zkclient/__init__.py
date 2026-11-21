"""Standard-library client for the zk-RISC-V proof service."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from http.client import HTTPException
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

JobStatus = Literal["queued", "running", "succeeded", "failed"]
_JOB_STATUSES = {"queued", "running", "succeeded", "failed"}
_WORD = re.compile(r"0x[0-9a-fA-F]{8}")
_TOKEN = re.compile(r"[A-Za-z0-9._~-]{24,256}")
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._~-]{1,128}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_JSON_BYTES = 32 * 1024
_MAX_PROOF_BYTES = 64 * 1024


class ClientError(Exception):
    """Transport failure or an invalid service response."""


class ServiceError(ClientError):
    """An HTTP error returned by the proof service."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code


@dataclass(frozen=True)
class JobError:
    code: str
    message: str


@dataclass(frozen=True)
class ProofResult:
    x5: int
    x6: int
    proof_sha256: str
    proof_size: int
    proof_url: str
    circuit_id: str

    def __post_init__(self) -> None:
        if any(type(value) is not int or not 0 <= value < 2**32 for value in (self.x5, self.x6)):
            raise ValueError("Invalid public register values")
        if (
            _SHA256.fullmatch(self.proof_sha256) is None
            or type(self.proof_size) is not int
            or not 1 <= self.proof_size <= _MAX_PROOF_BYTES
        ):
            raise ValueError("Invalid proof digest or size")


@dataclass(frozen=True)
class Job:
    id: str
    status: JobStatus
    instructions: tuple[str, ...]
    created_at: str
    updated_at: str
    attempts: int
    result: ProofResult | None
    error: JobError | None

    @classmethod
    def parse(cls, payload: bytes) -> Job:
        try:
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object")
            data["id"] = str(UUID(data["id"]))
            if data["status"] not in _JOB_STATUSES:
                raise ValueError("Unknown job state")
            instructions = data["instructions"]
            if type(instructions) is not list or len(instructions) != 4:
                raise ValueError("Expected four instructions")
            data["instructions"] = tuple(_canonical_word(word) for word in instructions)
            if data["result"] is not None:
                data["result"] = ProofResult(**data["result"])
            if data["error"] is not None:
                data["error"] = JobError(**data["error"])
            if (data["status"] == "succeeded") != (data["result"] is not None):
                raise ValueError("Missing or premature proof metadata")
            if (data["status"] == "failed") != (data["error"] is not None):
                raise ValueError("Missing or premature error metadata")
            return cls(**data)
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ClientError("Malformed job response") from error


def _canonical_word(word: str | int) -> str:
    if type(word) is int and 0 <= word < 2**32:
        return f"0x{word:08x}"
    if isinstance(word, str) and _WORD.fullmatch(word):
        return word.lower()
    raise ValueError("Expected an unsigned 32-bit integer or 0x-prefixed eight-digit word")


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0):
        url = urlsplit(base_url)
        if (
            url.scheme not in {"http", "https"}
            or url.hostname is None
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
        ):
            raise ValueError("Expected an HTTP(S) service origin or base path")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Request timeout must be positive and finite")
        if _TOKEN.fullmatch(token) is None:
            raise ValueError("Expected a 24–256 character URL-safe API token")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._token = token
        self._opener = build_opener(_RejectRedirects())

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        binary: bool = False,
    ) -> bytes:
        content_type = "application/octet-stream" if binary else "application/json"
        request_headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": content_type,
            **(headers or {}),
        }
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        request = Request(self.base_url + path, data=data, headers=request_headers, method=method)
        limit = _MAX_PROOF_BYTES if binary else _MAX_JSON_BYTES
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.headers.get_content_type() != content_type:
                    raise ClientError(f"Unexpected content type when reading {path}")
                body = response.read(limit + 1)
        except HTTPError as error:
            with error:
                service_error = _read_http_error(error)
            raise service_error from None
        except (URLError, HTTPException, OSError) as error:
            raise ClientError(f"Transport failure while requesting {path}") from error
        if len(body) > limit:
            raise ClientError(f"Response is larger than the supported limit for {path}")
        return body

    def submit(
        self,
        instructions: Sequence[str | int],
        *,
        idempotency_key: str | None = None,
    ) -> Job:
        if len(instructions) != 4:
            raise ValueError("Expected exactly four instructions")
        words = tuple(map(_canonical_word, instructions))
        headers = {}
        if idempotency_key is not None:
            if _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
                raise ValueError("Invalid idempotency key")
            headers["Idempotency-Key"] = idempotency_key
        body = json.dumps({"instructions": words}).encode("utf-8")
        job = Job.parse(self._request("POST", "/v1/jobs", data=body, headers=headers))
        if job.instructions != words:
            raise ClientError("Returned job contains a different program")
        return job

    def get(self, job_id: str) -> Job:
        canonical_id = str(UUID(job_id))
        job = Job.parse(self._request("GET", f"/v1/jobs/{canonical_id}"))
        if job.id != canonical_id:
            raise ClientError("Returned job ID differs from the request")
        return job

    def wait(self, job_id: str, *, timeout: float = 30.0, interval: float = 1.0) -> Job:
        if not math.isfinite(timeout) or not math.isfinite(interval) or timeout <= 0 or interval <= 0:
            raise ValueError("Poll timeout and interval must be positive and finite")
        expires = time.monotonic() + timeout
        while True:
            job = self.get(job_id)
            if job.status in {"succeeded", "failed"}:
                return job
            remaining = expires - time.monotonic()
            if remaining <= 0:
                raise ClientError(f"Timed out waiting for job {job.id}")
            time.sleep(min(interval, remaining))

    def download_proof(self, job_id: str) -> bytes:
        job = self.get(job_id)
        if job.result is None:
            raise ClientError(f"Proof is unavailable for job {job.id} in state {job.status}")
        # Use our origin and job ID instead of a potentially untrusted response URL.
        proof = self._request("GET", f"/v1/jobs/{job.id}/proof", binary=True)
        digest = hashlib.sha256(proof).hexdigest()
        if len(proof) != job.result.proof_size or digest != job.result.proof_sha256:
            raise ClientError(f"Downloaded proof failed integrity checks for job {job.id}")
        return proof


def _read_http_error(error: HTTPError) -> ServiceError:
    try:
        payload = json.loads(error.read(_MAX_JSON_BYTES + 1))
        details = payload["error"]
        if isinstance(details["code"], str) and isinstance(details["message"], str):
            return ServiceError(error.code, details["code"], details["message"])
    except (HTTPException, KeyError, OSError, TypeError, ValueError):
        pass
    return ServiceError(error.code, "http_error", "Unexpected service response")


__all__ = ["Client", "ClientError", "Job", "JobError", "ProofResult", "ServiceError"]
