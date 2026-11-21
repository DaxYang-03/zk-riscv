"""Run the existing RTL simulator and Halo2 prover in isolated scratch directories."""

import math
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

MAX_PROOF_BYTES = 65_536
WORD_COUNT = 4
U32_MAX = (1 << 32) - 1
_WORD = re.compile(r"(?:0[xX])?([0-9a-fA-F]{8})")
_FINAL_REGISTERS = re.compile(rb"^final_registers=x5:([0-9]{1,10}),x6:([0-9]{1,10})$", re.MULTILINE)


class InvalidProgram(ValueError):
    """The requested instruction words are outside the supported circuit."""


class EngineError(RuntimeError):
    """An operational failure safe to represent with a stable public code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ProofResult:
    proof: bytes
    x5: int
    x6: int


def normalize_program(words: Iterable[int | str]) -> tuple[int, ...]:
    program = []
    for word in words:
        if len(program) == WORD_COUNT:
            raise InvalidProgram("expected exactly four instruction words")
        if isinstance(word, str):
            match = _WORD.fullmatch(word)
            if match is None:
                raise InvalidProgram("instruction words must use eight hexadecimal digits")
            word = int(match.group(1), 16)
        if type(word) is not int or not 0 <= word <= U32_MAX:
            raise InvalidProgram("instruction words must be unsigned 32-bit integers")
        if word & 0x707F != 0x13:
            raise InvalidProgram("instruction words must encode RV32I ADDI")
        program.append(word)
    if len(program) != WORD_COUNT:
        raise InvalidProgram("expected exactly four instruction words")
    return tuple(program)


class Backend:
    """Invoke a fixed proof toolchain with a separate timeout for each phase."""

    def __init__(
        self, vvp: str, simulator: str | Path, prover: str | Path, timeout: float = 10.0
    ):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("engine timeout must be positive and finite")
        # Resolve configured paths before building commands for a temporary working directory.
        executable = shutil.which(os.path.expanduser(vvp))
        self.vvp = str(Path(executable).absolute()) if executable is not None else vvp
        self.simulator = Path(simulator).expanduser().resolve()
        self.prover = Path(prover).expanduser().resolve()
        self.timeout = timeout

    def validate(self) -> None:
        if (
            not shutil.which(self.vvp)
            or not self.simulator.is_file()
            or not self.prover.is_file()
            or not os.access(self.prover, os.X_OK)
        ):
            raise EngineError("engine_unavailable")

    def _run(self, command: list[str], scratch: Path) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                command,
                cwd=scratch,
                stdin=subprocess.DEVNULL,
                env={"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C"},
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise EngineError("engine_timeout") from exc
        except OSError as exc:
            raise EngineError("engine_unavailable") from exc

    def _verify_file(self, program: tuple[int, ...], x5: int, x6: int, path: Path) -> bool:
        words = [f"0x{word:08x}" for word in program]
        result = self._run(
            [str(self.prover), "--verify", str(path), *words, str(x5), str(x6)], path.parent
        )
        if result.returncode == 1:
            return False
        if result.returncode != 0 or b"verification=ok" not in result.stdout.splitlines():
            raise EngineError("verification_failed")
        return True

    def prove(self, program: tuple[int, ...]) -> ProofResult:
        program = normalize_program(program)
        self.validate()
        try:
            with tempfile.TemporaryDirectory(prefix="zk-riscv-") as directory:
                scratch = Path(directory).resolve()
                source = scratch / "program.hex"
                source.write_text("".join(f"{word:08x}\n" for word in program), encoding="ascii")
                execution = self._run(
                    [self.vvp, str(self.simulator), f"+program={source}"], scratch
                )
                if execution.returncode:
                    raise EngineError("execution_failed")
                trace = scratch / "trace.csv"
                trace.write_bytes(execution.stdout)
                proof_path = scratch / "proof.bin"
                prover = self._run([str(self.prover), str(trace), str(proof_path)], scratch)
                registers = _FINAL_REGISTERS.search(prover.stdout)
                if prover.returncode or registers is None or not proof_path.is_file():
                    raise EngineError("prover_failed")
                x5, x6 = map(int, registers.groups())
                if max(x5, x6) > U32_MAX or not 0 < proof_path.stat().st_size <= MAX_PROOF_BYTES:
                    raise EngineError("prover_failed")
                if not self._verify_file(program, x5, x6, proof_path):
                    raise EngineError("verification_failed")
                return ProofResult(proof_path.read_bytes(), x5, x6)
        except OSError as exc:
            raise EngineError("engine_unavailable") from exc

    def verify(self, program: tuple[int, ...], x5: int, x6: int, proof: bytes) -> bool:
        program = normalize_program(program)
        if any(type(value) is not int or not 0 <= value <= U32_MAX for value in (x5, x6)):
            raise InvalidProgram("register values must be unsigned 32-bit integers")
        if not 0 < len(proof) <= MAX_PROOF_BYTES:
            return False
        try:
            with tempfile.TemporaryDirectory(prefix="zk-riscv-verify-") as directory:
                path = Path(directory).resolve() / "proof.bin"
                path.write_bytes(proof)
                return self._verify_file(program, x5, x6, path)
        except OSError as exc:
            raise EngineError("engine_unavailable") from exc
