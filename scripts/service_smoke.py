"""Exercise a real proof-service process through HTTP and a local Rust verifier."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from zkclient import Client, Job, ServiceError

CANONICAL = ("0x00700293", "0xffd28293", "0xfff00313", "0x00230313")
ALTERNATE = ("0xfff00293", "0x00328293", "0xffc28313", "0x00730013")


def require(check: bool, message: str) -> None:
    if not check:
        raise RuntimeError(message)


def ready(base_url: str, process: subprocess.Popen | None = None) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"Service exited before opening {base_url} (code {process.returncode})")
        try:
            with urlopen(base_url + "/readyz", timeout=1) as response:
                if response.status == 200 and json.load(response) == {"status": "ok"}:
                    return
        except (TimeoutError, URLError, ValueError):
            pass
        time.sleep(0.15)
    raise RuntimeError(f"Service did not become ready at {base_url}")


@contextlib.contextmanager
def run_server(env: dict[str, str], base_url: str, log_path: Path) -> Iterator[None]:
    with log_path.open("w+b") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "zkservice"],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            ready(base_url, process)
            yield
        except Exception:
            log.seek(max(log.seek(0, 2) - 8192, 0))
            print(log.read().decode("utf-8", errors="replace"), file=sys.stderr)
            raise
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def check_proof(
    prover: Path,
    path: Path,
    instructions: tuple[str, ...],
    expected: tuple[int, int],
    *,
    valid: bool,
) -> None:
    result = subprocess.run(
        [str(prover), "--verify", str(path), *instructions, *map(str, expected)],
        capture_output=True,
        timeout=15,
        check=False,
    )
    accepted = result.returncode == 0 and b"verification=ok" in result.stdout
    require(accepted if valid else result.returncode != 0, f"Unexpected verifier result for {path.name}")


def check_job(
    client: Client,
    prover: Path,
    directory: Path,
    instructions: tuple[str, ...],
    expected: tuple[int, int],
    key: str,
) -> tuple[Job, bytes]:
    original = client.submit(instructions, idempotency_key=key)
    replay = client.submit(instructions, idempotency_key=key)
    require(replay.id == original.id, "Idempotent submission created a second job")
    job = client.wait(original.id)
    if job.status != "succeeded" or job.result is None:
        raise RuntimeError(f"Job {job.id} did not finish successfully: {job.error}")
    require((job.result.x5, job.result.x6) == expected, "Incorrect final register values")
    proof = client.download_proof(job.id)
    path = directory / f"{job.id}.bin"
    path.write_bytes(proof)
    check_proof(prover, path, instructions, expected, valid=True)
    return job, proof


def exercise(base_url: str, token: str, prover: Path, directory: Path) -> tuple[Client, str, bytes]:
    client = Client(base_url, token)
    try:
        Client(base_url, secrets.token_urlsafe(32)).submit(CANONICAL)
    except ServiceError as error:
        require(error.status == 401, f"Incorrect unauthorized response: {error}")
    else:
        raise RuntimeError("An unauthenticated submission was accepted")

    key = secrets.token_hex(16)
    canonical, proof = check_job(client, prover, directory, CANONICAL, (4, 1), key)
    try:
        client.submit(ALTERNATE, idempotency_key=key)
    except ServiceError as error:
        require(error.status == 409, f"Incorrect idempotency conflict response: {error}")
    else:
        raise RuntimeError("The service accepted a different program under the same idempotency key")

    check_job(client, prover, directory, ALTERNATE, (2, 4294967294), secrets.token_hex(16))
    altered = directory / "altered.bin"
    altered.write_bytes(bytes((proof[0] ^ 1,)) + proof[1:])
    check_proof(prover, altered, CANONICAL, (4, 1), valid=False)
    print("PASS: authentication, idempotency, two RTL executions and independent proof verification")
    return client, canonical.id, proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="test an already-running service and skip the restart check")
    parser.add_argument("--prover", type=Path, default=Path("build/cargo/release/zk-riscv-proof"))
    args = parser.parse_args()
    prover = args.prover.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="zkservice-smoke-") as tmp:
        directory = Path(tmp)
        if args.url:
            token = os.environ.get("ZKR_API_TOKEN")
            if not token:
                parser.error("ZKR_API_TOKEN is required for an external service")
            ready(args.url)
            exercise(args.url, token, prover, directory)
            return

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        token = secrets.token_urlsafe(32)
        env = {
            **os.environ,
            "ZKR_API_TOKEN": token,
            "ZKR_DB_PATH": str(directory / "jobs.sqlite3"),
            "ZKR_HOST": "127.0.0.1",
            "ZKR_PORT": str(port),
            "ZKR_PROVER": str(prover),
        }
        with run_server(env, base_url, directory / "first.log"):
            client, job_id, proof = exercise(base_url, token, prover, directory)
        with run_server(env, base_url, directory / "restart.log"):
            require(client.download_proof(job_id) == proof, "Restart changed the downloaded proof")
        print("PASS: persisted proof and metadata survive restart")


if __name__ == "__main__":
    main()
