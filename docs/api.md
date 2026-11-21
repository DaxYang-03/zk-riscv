# Proof service API

The versioned HTTP API exposes a single operator account through a bearer token. Every route under `/v1` requires an `Authorization: Bearer <token>` header. The running service serves its OpenAPI schema at `/openapi.json` and interactive documentation at `/docs`.

## Submit and retrieve a job

```sh
curl --fail-with-body -sS http://127.0.0.1:8080/v1/jobs \
  -H "Authorization: Bearer $ZKR_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: canonical-v1' \
  --data '{"instructions":["0x00700293","0xffd28293","0xfff00313","0x00230313"]}'
```

The request takes exactly four 32-bit instruction words as `0x` followed by eight hexadecimal digits. All four must encode RV32I `ADDI`. A new submission returns HTTP 202 and a job URL in `Location`. An idempotent replay returns HTTP 200 and the same job, including its current state. A key already bound to another program returns HTTP 409. Keys accept up to 128 characters from `A–Z`, `a–z`, `0–9`, and `._~-`.

| Endpoint | Response |
| --- | --- |
| `GET /v1/capabilities` | Circuit identifier, supported instructions, proof format, and public input order |
| `POST /v1/jobs` | Job identifier, canonical program, status, attempt count, and timestamps |
| `GET /v1/jobs/{id}` | Current job state and, when complete, its proof metadata |
| `GET /v1/jobs/{id}/proof` | Binary proof with `X-Proof-SHA256` and `X-ZKR-Circuit` headers |
| `GET /healthz` | Process liveness without an operator token |
| `GET /readyz` | Worker and database readiness without an operator token |

A submitted job moves from `queued` to `running` and then to either `succeeded` or `failed`. A worker lease allows interrupted work to be reclaimed after expiration. Wait for `succeeded` before fetching the proof. Pending job responses include `Retry-After: 1`. Completed job metadata contains `x5`, `x6`, `proof_size`, `proof_sha256`, `proof_url`, and `circuit_id`.

| HTTP status | Client handling |
| --- | --- |
| `401` | Check operator credentials |
| `404` | Check the job ID and retention policy |
| `409` | Read `error.code` to distinguish a conflicting key from an unavailable proof |
| `413` | Keep the request body at or below 4,096 bytes |
| `422` | Check the schema and instruction encodings |
| `429` | Retry `queue_full` after the indicated delay. Send `storage_full` to the operator for retention cleanup. |
| `503` | Check service readiness before retrying |

Application errors use `{"error":{"code":"…","message":"…"}}`. A retry after an ambiguous submission should use the same idempotency key. Retention deletes completed jobs, their proofs, and their idempotency keys together.

## Python SDK

Add `service/` to `PYTHONPATH` to use the standard-library client directly from a checkout, or use the client included in the service wheel. Create one idempotency key per logical submission and retain it across retries.

```python
import os
from pathlib import Path
from zkclient import Client

client = Client("http://127.0.0.1:8080", os.environ["ZKR_API_TOKEN"])
job = client.submit(
    [0x00700293, 0xFFD28293, 0xFFF00313, 0x00230313],
    idempotency_key="canonical-v1",
)
job = client.wait(job.id)
if job.result is None:
    raise RuntimeError(f"Proof job failed: {job.error}")
Path("proof.bin").write_bytes(client.download_proof(job.id))
```

Use `python -m zkclient --help` for the CLI. Set `ZKR_API_URL` for a different service origin.

## Verify a receipt independently

The download digest confirms that the client received the bytes associated with the job record. To establish execution validity, use the local verifier with the program and outputs expected by your application.

```sh
make service-build
build/cargo/release/zk-riscv-proof --verify \
  proof.bin 0x00700293 0xffd28293 0xfff00313 0x00230313 4 1
```

The public inputs for `rv32i-addi-4/halo2-ipa-pasta/v1` are the four instruction words in order, then final `x5` and `x6`. Follow the same binding when verifying receipts from custom programs. [Proof statement and trust model](design.md).
