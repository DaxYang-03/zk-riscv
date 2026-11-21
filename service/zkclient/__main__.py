"""Command-line interface for the operator SDK."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from zkclient import Client, ClientError


def _submission_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("instructions", metavar="WORD", nargs=4, help="four 0x-prefixed ADDI words")
    parser.add_argument("--idempotency-key", help="reuse a key to replay the same submission")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("ZKR_API_URL", "http://127.0.0.1:8080"))
    actions = parser.add_subparsers(dest="action", required=True)
    submit = actions.add_parser("submit", help="submit a program and print its job")
    _submission_options(submit)
    status = actions.add_parser("status", help="get a job and its public result")
    status.add_argument("job_id")
    proof = actions.add_parser("proof", help="download a completed proof")
    proof.add_argument("job_id")
    proof.add_argument("-o", "--output", type=Path, required=True)
    prove = actions.add_parser("prove", help="submit, wait and download a proof")
    _submission_options(prove)
    prove.add_argument("-o", "--output", type=Path, required=True)
    prove.add_argument("--wait", type=float, default=30.0)
    args = parser.parse_args()

    token = os.environ.get("ZKR_API_TOKEN")
    if not token:
        parser.error("ZKR_API_TOKEN must be set")
    try:
        client = Client(args.url, token)
        if args.action in {"submit", "prove"}:
            job = client.submit(args.instructions, idempotency_key=args.idempotency_key)
            if args.action == "prove":
                job = client.wait(job.id, timeout=args.wait)
        else:
            job = client.get(args.job_id)
        if args.action in {"proof", "prove"}:
            if job.status == "failed" and job.error:
                raise ClientError(f"Job {job.id} failed: {job.error.code}")
            content = client.download_proof(job.id)
            with args.output.open("xb") as output:
                output.write(content)
        print(json.dumps(asdict(job), indent=2))
    except (ClientError, OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
