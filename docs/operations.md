# Proof service operations

Run one API process and its embedded worker per SQLite database. Use a persistent local disk for the SQLite database and its write-ahead log. Place a TLS reverse proxy in front of the API when clients connect over a network. Keep the bearer token in an operator-controlled secret store and inject it at startup.

## Container deployment

```sh
docker build --tag zk-riscv:local .
export ZKR_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker run --detach --name zk-riscv --read-only \
  --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid \
  --publish 127.0.0.1:8080:8080 \
  --env ZKR_API_TOKEN --volume zk-riscv-data:/var/lib/zk-riscv \
  zk-riscv:local
curl --fail --silent http://127.0.0.1:8080/readyz
```

The image runs as UID and GID 10001. The named volume preserves jobs and proofs when the container is replaced. For a bind mount, ensure the directory is writable by that UID. The proof worker uses the temporary filesystem for the instructions, trace, and proof before the artifact is committed to SQLite.

## Configuration

| Environment variable | Default or behavior |
| --- | --- |
| `ZKR_API_TOKEN` | Required. Accepts 24–256 URL-safe ASCII characters. |
| `ZKR_HOST`, `ZKR_PORT` | `127.0.0.1:8080` locally. The container binds `0.0.0.0:8080` inside its network namespace. |
| `ZKR_DB_PATH` | `build/service/jobs.sqlite3` locally and `/var/lib/zk-riscv/jobs.sqlite3` in the image. |
| `ZKR_VVP` | Icarus VVP executable. |
| `ZKR_SIMULATOR` | Compiled trace testbench at `build/tb_trace.vvp` locally. |
| `ZKR_PROVER` | Native prover and verifier at `build/cargo/release/zk-riscv-proof` locally. |
| `ZKR_STAGE_TIMEOUT_SECONDS` | 10 seconds per subprocess. |
| `ZKR_MAX_LIVE_JOBS` | 128 queued and running jobs. |
| `ZKR_MAX_STORED_JOBS` | 10,000 persisted jobs across all states. |
| `ZKR_MAX_ATTEMPTS` | 3 lease claims before an interrupted job reaches the attempt limit. |

The Docker image sets the simulator and verifier paths at build time. The local `make serve` target uses the paths created by its build prerequisites.

## Monitor and restart

Check `/healthz` for a responsive process and `/readyz` for a live worker and accessible database. Inspect worker logs using `docker logs zk-riscv` and use the job ID to correlate a delivery with a failure code. A full active queue returns HTTP 429 with `queue_full` and a retry delay. A full database returns HTTP 429 with `storage_full` and requires retention cleanup.

Allow the current job to finish when shutting down. With the default stage timeout, `docker stop --timeout 45 zk-riscv` provides time for the three subprocesses. Increase the shutdown grace period if you raise the per-stage timeout. After an abrupt interruption, a replacement process can reclaim unfinished work once its lease expires. The operator can confirm the job state through the API.

## Back up and prune

Stop the service before offline maintenance. Back up the database with SQLite's backup command, using a destination on a separate disk. For a container, mount its data volume into a maintenance container under the same UID.

```sh
sqlite3 build/service/jobs.sqlite3 ".backup 'proof-jobs.backup.sqlite3'"
uv run --locked --project service python -m zkservice.admin prune \
  --database build/service/jobs.sqlite3 --older-than-hours 168
```

Download artifacts you need to retain before pruning. Review the dry-run count, then repeat the command with `--apply`. Pruning removes eligible succeeded and failed jobs, including their proofs and idempotency keys. SQLite can reuse released pages for new jobs.

## Validate a deployment

`make service-smoke` launches a local API process, submits two programs, checks idempotency and authentication, verifies the downloaded proofs independently, rejects a tampered artifact, and checks that a proof survives restart. The [Proof service workflow](../.github/workflows/service.yml) runs the native tests and a container smoke from a clean build.

The current deployment profile is a single-node, single-operator proof service for the documented four-step circuit. [API contract](api.md) · [Circuit and trust model](design.md).
