#!/usr/bin/env python3
"""Run the checked-in engineering gates and record their local evidence."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
VERSION_COMMANDS = {
    "iverilog": ("IVERILOG", "-V"),
    "vvp": ("VVP", "-V"),
    "yosys": ("YOSYS", "-V"),
    "cargo": ("CARGO", "--version"),
    "rustc": ("RUSTC", "--version"),
}
CHECKS = ("test-rtl", "test-proof", "formal", "demo")
ARTIFACTS = ("trace.csv", "proof.bin", "formal.log")
SOURCE_PATTERNS = (
    "Makefile",
    "fixtures/demo_trace.csv",
    "formal/*.sv",
    "formal/*.ys",
    "proof/Cargo.toml",
    "proof/Cargo.lock",
    "proof/src/**/*.rs",
    "proof/tests/**/*.rs",
    "rtl/*.sv",
    "scripts/run_evidence.py",
)


def tool_version(tool, override, flag):
    try:
        result = subprocess.run(
            [os.environ.get(override, tool), flag],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"error": str(error)}

    lines = result.stdout.splitlines()
    return {"exit_code": result.returncode, "version": lines[0] if lines else None}


def run_check(target, run_dir):
    command = ["make", f"BUILD_DIR={run_dir}", target]
    log_name = f"check-{target}.log"
    started = time.monotonic()
    print(f"Running {target}...", flush=True)

    with (ROOT / run_dir / log_name).open("w", encoding="utf-8") as log:
        result = subprocess.run(
            command,
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )

    status = "passed" if result.returncode == 0 else "failed"
    print(f"{target} {status} ({run_dir / log_name})", flush=True)
    return {
        "target": target,
        "command": command,
        "exit_code": result.returncode,
        "status": status,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "log": log_name,
    }


def describe_file(path, base):
    content = (base / path).read_bytes()
    return {
        "path": path,
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def git_metadata():
    try:
        invocation = {"cwd": ROOT, "stderr": subprocess.DEVNULL, "text": True, "timeout": 10}
        top = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], **invocation)
        if Path(top.strip()).resolve() != ROOT:
            return None
        status = subprocess.check_output(["git", "status", "--porcelain=v2", "--branch"], **invocation)
    except (OSError, subprocess.SubprocessError):
        return None

    lines = status.splitlines()
    oid_prefix = "# branch.oid "
    oid = next((line[len(oid_prefix):] for line in lines if line.startswith(oid_prefix)), "(initial)")
    return {
        "revision": None if oid == "(initial)" else oid,
        "dirty": any(not line.startswith("#") for line in lines),
    }


def capture_sources():
    paths = sorted(
        path.relative_to(ROOT).as_posix()
        for pattern in SOURCE_PATTERNS
        for path in ROOT.glob(pattern)
    )
    return [describe_file(path, ROOT) for path in paths]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", default="build")
    args = parser.parse_args()

    evidence_dir = ROOT / args.build_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=evidence_dir))
    if run_dir.is_relative_to(ROOT):
        run_dir = run_dir.relative_to(ROOT)

    tools = {
        tool: tool_version(tool, override, flag)
        for tool, (override, flag) in VERSION_COMMANDS.items()
    }
    tools["python"] = {"version": sys.version.split()[0]}

    sources = capture_sources()
    git = git_metadata()
    checks = [run_check(target, run_dir) for target in CHECKS]
    sources_unchanged = sources == capture_sources()
    artifacts = [
        describe_file(path, ROOT / run_dir)
        for path in ARTIFACTS
        if (ROOT / run_dir / path).is_file()
    ]
    missing_artifacts = sorted(set(ARTIFACTS) - {artifact["path"] for artifact in artifacts})
    passed = (
        all(check["status"] == "passed" for check in checks)
        and sources_unchanged
        and not missing_artifacts
    )
    report = {
        "schema_version": 1,
        "status": "passed" if passed else "failed",
        "tools": tools,
        "git": git,
        "sources": sources,
        "sources_unchanged": sources_unchanged,
        "checks": checks,
        "artifacts": artifacts,
        "missing_artifacts": missing_artifacts,
    }
    report_path = run_dir / "report.json"
    (ROOT / report_path).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Evidence report: {report_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
