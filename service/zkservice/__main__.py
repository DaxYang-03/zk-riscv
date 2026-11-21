"""Run a single HTTP process with its internal proof worker."""

import sys

import uvicorn

from .app import create_app
from .config import Settings


def main() -> None:
    try:
        settings = Settings.from_environment()
    except ValueError as error:
        print(f"Invalid service configuration. {error}", file=sys.stderr)
        raise SystemExit(2) from None
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        proxy_headers=False,
        server_header=False,
        workers=1,
        limit_concurrency=128,
        backlog=128,
        timeout_keep_alive=5,
    )


if __name__ == "__main__":
    main()
