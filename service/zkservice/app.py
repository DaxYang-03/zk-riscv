"""HTTP endpoints and lifecycle for the proof service."""

import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hmac import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, Request, Response, Security
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.exceptions import HTTPException

from . import __version__
from .backend import MAX_PROOF_BYTES, Backend, EngineError, InvalidProgram, normalize_program
from .config import Settings
from .schemas import (
    CIRCUIT_ID,
    CapabilitiesResponse,
    ErrorDetail,
    ErrorResponse,
    HealthResponse,
    JobRequest,
    JobResponse,
    JobResult,
)
from .store import Busy, Conflict, Job, Store
from .transport import ApiError, RequestPolicy, error_response
from .worker import Worker

LOGGER = logging.getLogger(__name__)
BODY_LIMIT = 4_096
PROOF_TYPE = "application/octet-stream"
ERROR_SCHEMA = {"model": ErrorResponse}
POLL_HINT = {"Retry-After": "1"}
BEARER = HTTPBearer(auto_error=False, scheme_name="OperatorBearer")
FAILURE_MESSAGES = {
    "engine_timeout": "A proof stage exceeded its time limit",
    "engine_unavailable": "The proof engine is unavailable",
    "execution_failed": "The simulator failed to execute the program",
    "prover_failed": "The prover failed to produce a proof",
    "verification_failed": "The proof did not pass independent verification",
    "attempts_exhausted": "The job reached its retry limit",
}


def job_response(request: Request, job: Job) -> JobResponse:
    result = None
    error = None
    if job.status == "succeeded":
        if job.x5 is None or job.x6 is None or job.proof_sha256 is None or job.proof_size is None:
            raise ApiError(503, "storage_unavailable", "Proof metadata is unavailable")
        result = JobResult(
            x5=job.x5,
            x6=job.x6,
            proof_sha256=job.proof_sha256,
            proof_size=job.proof_size,
            proof_url=request.url_for("get_proof", job_id=job.id).path,
            circuit_id=CIRCUIT_ID,
        )
    if job.status == "failed":
        code = job.error_code or "job_failed"
        error = ErrorDetail(code=code, message=FAILURE_MESSAGES.get(code, "The proof job failed"))
    return JobResponse(
        id=job.id,
        status=job.status,
        instructions=tuple(f"0x{word:08x}" for word in job.program),
        created_at=job.created_at,
        updated_at=job.updated_at,
        attempts=job.attempts,
        result=result,
        error=error,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings.from_environment()
    token = config.api_token.encode("ascii")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        backend = Backend(config.vvp, config.simulator, config.prover, config.stage_timeout_seconds)
        try:
            backend.validate()
        except EngineError:
            raise RuntimeError("Proof engine unavailable. Check the simulator and prover configuration.") from None
        config.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        app.state.store = Store(
            config.database,
            max_live_jobs=config.max_live_jobs,
            max_stored_jobs=config.max_stored_jobs,
            max_attempts=config.max_attempts,
        )
        app.state.worker = Worker(app.state.store, backend)
        app.state.worker.start()
        try:
            yield
        finally:
            await run_in_threadpool(app.state.worker.stop)

    app = FastAPI(
        title="zk-RISC-V Proof Service",
        version=__version__,
        description="Submit four RV32I ADDI instructions to produce an independently verifiable Halo2 proof.",
        lifespan=lifespan,
        redoc_url=None,
        swagger_ui_parameters={"persistAuthorization": False},
    )
    app.add_middleware(RequestPolicy, max_body_bytes=BODY_LIMIT)

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, error: ApiError) -> Response:
        return error_response(error)

    @app.exception_handler(RequestValidationError)
    async def handle_invalid_request(request: Request, error: RequestValidationError) -> Response:
        return error_response(ApiError(422, "invalid_request", "Request does not match the API schema"))

    @app.exception_handler(HTTPException)
    async def handle_http_error(request: Request, error: HTTPException) -> Response:
        if error.status_code == 404:
            code, message = "not_found", "Endpoint not found"
        elif error.status_code == 405:
            code, message = "method_not_allowed", "Method not allowed"
        else:
            code, message = "request_rejected", "Request rejected"
        return error_response(ApiError(error.status_code, code, message, error.headers))

    @app.exception_handler(sqlite3.Error)
    async def handle_database_error(request: Request, error: sqlite3.Error) -> Response:
        LOGGER.warning("Storage operation failed (%s)", type(error).__name__)
        return error_response(ApiError(503, "storage_unavailable", "Job storage is unavailable"))

    @app.get("/healthz", tags=["Operations"])
    def live() -> HealthResponse:
        return HealthResponse()

    @app.get("/readyz", tags=["Operations"], responses={503: ERROR_SCHEMA})
    def ready(request: Request) -> HealthResponse:
        if not request.app.state.worker.alive:
            raise ApiError(503, "worker_unavailable", "Proof worker is unavailable")
        request.app.state.store.ping()
        return HealthResponse()

    def authorize(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(BEARER)],
    ) -> None:
        candidate = credentials.credentials.encode("latin-1") if credentials else b""
        valid = compare_digest(token, candidate)
        if not valid or len(request.headers.getlist("authorization")) != 1:
            raise ApiError(401, "unauthorized", "A valid bearer token is required", {"WWW-Authenticate": "Bearer"})

    router = APIRouter(
        prefix="/v1",
        tags=["Proof jobs"],
        dependencies=[Depends(authorize)],
        responses={401: ERROR_SCHEMA, 422: ERROR_SCHEMA, 503: ERROR_SCHEMA},
    )

    @router.get("/capabilities")
    def capabilities() -> CapabilitiesResponse:
        return CapabilitiesResponse(
            circuit_id=CIRCUIT_ID,
            steps=4,
            isa="RV32I",
            supported_instructions=("ADDI",),
            public_inputs=("instruction_0", "instruction_1", "instruction_2", "instruction_3", "x5", "x6"),
            proof_content_type=PROOF_TYPE,
            max_proof_bytes=MAX_PROOF_BYTES,
        )

    @router.post(
        "/jobs",
        status_code=202,
        responses={200: {"model": JobResponse}, 409: ERROR_SCHEMA, 413: ERROR_SCHEMA, 429: ERROR_SCHEMA},
    )
    def submit_job(
        payload: JobRequest,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str | None, Header(pattern=r"^[A-Za-z0-9._~-]{1,128}$")] = None,
    ) -> JobResponse:
        if len(request.headers.getlist("idempotency-key")) > 1:
            raise ApiError(422, "invalid_request", "Provide a single Idempotency-Key header")
        if not request.app.state.worker.alive:
            raise ApiError(503, "worker_unavailable", "Proof worker is unavailable")
        try:
            program = normalize_program(payload.instructions)
            job, created = request.app.state.store.submit(program, idempotency_key)
        except InvalidProgram:
            raise ApiError(422, "unsupported_program", "Each instruction must encode RV32I ADDI") from None
        except Conflict:
            raise ApiError(409, "idempotency_conflict", "Idempotency key is bound to a different program") from None
        except Busy as error:
            if error.code == "queue_full":
                raise ApiError(429, "queue_full", "The live job queue is full", POLL_HINT) from None
            raise ApiError(429, "storage_full", "The job retention limit has been reached") from None
        if created:
            request.app.state.worker.notify()
        response.status_code = 202 if created else 200
        response.headers["Location"] = request.url_for("get_job", job_id=job.id).path
        if job.status in ("queued", "running"):
            response.headers.update(POLL_HINT)
        return job_response(request, job)

    def lookup_job(request: Request, job_id: UUID) -> Job:
        job = request.app.state.store.get(str(job_id))
        if job is None:
            raise ApiError(404, "job_not_found", "Job not found")
        return job

    @router.get("/jobs/{job_id}", responses={404: ERROR_SCHEMA})
    def get_job(job_id: UUID, request: Request, response: Response) -> JobResponse:
        job = lookup_job(request, job_id)
        if job.status in ("queued", "running"):
            response.headers.update(POLL_HINT)
        return job_response(request, job)

    @router.get(
        "/jobs/{job_id}/proof",
        response_class=Response,
        responses={
            200: {"content": {PROOF_TYPE: {"schema": {"type": "string", "format": "binary"}}}},
            404: ERROR_SCHEMA,
            409: ERROR_SCHEMA,
        },
    )
    def get_proof(job_id: UUID, request: Request) -> Response:
        job = lookup_job(request, job_id)
        if job.status != "succeeded":
            raise ApiError(409, "proof_unavailable", "The job has no completed proof")
        proof = request.app.state.store.artifact(job.id)
        if proof is None or job.proof_sha256 is None:
            raise ApiError(503, "storage_unavailable", "Proof artifact is unavailable")
        return Response(
            proof,
            media_type=PROOF_TYPE,
            headers={
                "Content-Disposition": f'attachment; filename="{job.id}.proof.bin"',
                "X-Proof-SHA256": job.proof_sha256,
                "X-ZKR-Circuit": CIRCUIT_ID,
            },
        )

    app.include_router(router)
    return app
