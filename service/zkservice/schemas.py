"""Public API schemas for statements, jobs, and downloadable proofs."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

CIRCUIT_ID = "rv32i-addi-4/halo2-ipa-pasta/v1"
UInt32 = Annotated[int, Field(ge=0, le=0xFFFFFFFF)]
InstructionWord = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^0x[0-9a-fA-F]{8}$"),
]
InstructionWords = tuple[InstructionWord, InstructionWord, InstructionWord, InstructionWord]


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instructions: InstructionWords = Field(
        examples=[("0x00700293", "0xffd28293", "0xfff00313", "0x00230313")]
    )


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class JobResult(BaseModel):
    x5: UInt32
    x6: UInt32
    proof_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    proof_size: Annotated[int, Field(gt=0)]
    proof_url: str
    circuit_id: str


class JobResponse(BaseModel):
    id: UUID
    status: Literal["queued", "running", "succeeded", "failed"]
    instructions: InstructionWords
    created_at: datetime
    updated_at: datetime
    attempts: Annotated[int, Field(ge=0)]
    result: JobResult | None = None
    error: ErrorDetail | None = None


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class CapabilitiesResponse(BaseModel):
    circuit_id: str
    steps: int
    isa: str
    supported_instructions: tuple[str, ...]
    public_inputs: tuple[str, ...]
    proof_content_type: str
    max_proof_bytes: int
