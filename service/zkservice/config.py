"""Parse service settings without exposing operator secrets in diagnostics."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~-]{24,256}")


def _environment_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} needs an integer between {minimum} and {maximum}") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} needs an integer between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    api_token: str = field(repr=False)
    database: Path = Path("build/service/jobs.sqlite3")
    vvp: str = "vvp"
    simulator: Path = Path("build/tb_trace.vvp")
    prover: Path = Path("build/cargo/release/zk-riscv-proof")
    stage_timeout_seconds: int = 10
    max_live_jobs: int = 128
    max_stored_jobs: int = 10_000
    max_attempts: int = 3
    host: str = "127.0.0.1"
    port: int = 8080

    def __post_init__(self) -> None:
        if TOKEN_PATTERN.fullmatch(self.api_token) is None:
            raise ValueError("ZKR_API_TOKEN needs 24–256 URL-safe ASCII characters")
        if self.max_live_jobs > self.max_stored_jobs:
            raise ValueError("ZKR_MAX_LIVE_JOBS must be <= ZKR_MAX_STORED_JOBS")

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            api_token=os.getenv("ZKR_API_TOKEN", ""),
            database=Path(os.getenv("ZKR_DB_PATH", "build/service/jobs.sqlite3")),
            vvp=os.getenv("ZKR_VVP", "vvp"),
            simulator=Path(os.getenv("ZKR_SIMULATOR", "build/tb_trace.vvp")),
            prover=Path(os.getenv("ZKR_PROVER", "build/cargo/release/zk-riscv-proof")),
            stage_timeout_seconds=_environment_int("ZKR_STAGE_TIMEOUT_SECONDS", 10, 1, 300),
            max_live_jobs=_environment_int("ZKR_MAX_LIVE_JOBS", 128, 1, 10_000),
            max_stored_jobs=_environment_int("ZKR_MAX_STORED_JOBS", 10_000, 1, 100_000),
            max_attempts=_environment_int("ZKR_MAX_ATTEMPTS", 3, 1, 10),
            host=os.getenv("ZKR_HOST", "127.0.0.1"),
            port=_environment_int("ZKR_PORT", 8080, 1, 65_535),
        )
