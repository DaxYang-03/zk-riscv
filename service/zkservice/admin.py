"""Inspect and prune the terminal history of a local proof-service database."""

import argparse
import json
import math
import sqlite3
import time
from pathlib import Path

from .store import Store


def _positive_hours(value: str) -> float:
    hours = float(value)
    if not math.isfinite(hours * 3600) or hours <= 0:
        raise argparse.ArgumentTypeError("age must be positive and finite")
    return hours


def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prune = commands.add_parser("prune", help="prune terminal jobs older than an age threshold")
    prune.add_argument("--database", required=True, type=Path, help="path to an existing database")
    prune.add_argument("--older-than-hours", required=True, type=_positive_hours)
    prune.add_argument("--apply", action="store_true", help="delete the matched jobs and proofs")
    options = parser.parse_args(args)

    if not options.database.expanduser().is_file():
        parser.error("the database file does not exist")
    try:
        store = Store(options.database, mode="rw" if options.apply else "ro")
        before = time.time() - options.older_than_hours * 3600
        count = store.prune_terminal(before, apply=options.apply)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.error(f"unable to prune database ({type(exc).__name__})")

    print(json.dumps({"mode": "apply" if options.apply else "dry-run", "terminal_jobs": count}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
