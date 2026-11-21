# zk-RISC-V

zk-RISC-V proves bounded execution of a synthesizable RV32I subset with Halo2. The expanded VM covers `ADDI`, `LW`, `SW`, `BEQ`, and `BNE`. Its circuit binds instruction fetches to a public ROM, constrains the program counter and register file, and checks the consistency of an address-and-time-sorted memory history.

**Bounded VM profile** · 8 retirements · 16 public ROM words · 16 data words · 32 registers · native verifier

## Run the bounded VM

Install GNU Make, Icarus Verilog, Rust 1.88 with Cargo, Python 3.11 or newer, and Yosys for synthesis checks.

```sh
make vm-demo
make vm-check
```

The [fixture program](fixtures/vm_program.hex) writes `17`, reads it into `x5`, and takes a forward `BEQ` that skips an instruction. It then overwrites the same data word with `20`, reads it into `x6`, and takes a backward `BNE`. After eight retirements the public statement is `PC = 28, x5 = 17, x6 = 20`.

```sh
cargo run --locked --release --manifest-path vm/Cargo.toml -- \
  verify fixtures/vm_program.hex build/vm-proof.bin 28 17 20
```

The fixture produces a 17,696-byte proof. The verifier uses the proof and the 19 public values encoded by the ROM and final-state arguments. `make vm-fuzz` checks 23 directed programs and 512 seeded random programs by default. It compares each retirement and trap with an independent Python execution model. `make vm-proof-fuzz` additionally produces and verifies Halo2 proofs for 16 sampled programs. The targets write coverage reports to `build/vm-fuzz.json` and `build/vm-proof-fuzz.json`.

[Execution, memory constraints, and trace format](docs/vm.md)

## Run the legacy ADDI proof service

The HTTP API continues to serve the four-step ADDI profile. It persists authenticated jobs and proof artifacts, and the Python SDK can download receipts for offline verification.

Install GNU Make, Icarus Verilog, Rust 1.88 with Cargo, Python 3.11 or newer, and `uv`. From the repository root, generate an operator token and start the service.

```sh
export ZKR_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
make serve
```

Use the same token in a second terminal to submit a job and download its proof. The SDK checks the download against the size and SHA-256 in the job result.

```sh
PYTHONPATH=service python3 -m zkclient prove \
  0x00700293 0xffd28293 0xfff00313 0x00230313 \
  --idempotency-key first-proof --output build/service-proof.bin

build/cargo/release/zk-riscv-proof --verify \
  build/service-proof.bin 0x00700293 0xffd28293 0xfff00313 0x00230313 4 1
```

The service listens on `127.0.0.1:8080` and stores jobs in `build/service/jobs.sqlite3` by default. The running service publishes interactive API documentation at `/docs` and its schema at `/openapi.json`.

[HTTP API and Python SDK](docs/api.md) · [Container deployment and operations](docs/operations.md)

## Run the four-step ADDI pipeline directly

Install GNU Make, Icarus Verilog (`iverilog` and `vvp`), Yosys, and Rust with Cargo. The CI workflow targets Ubuntu 24.04 and Rust 1.88.0. The evidence collector also requires Python 3.9 or newer.

```sh
make doctor
make demo
```

The demo writes `build/trace.csv` and `build/proof.bin`. The native verifier reports a 7,872-byte proof and successful verification for this circuit.

```text
proof_bytes=7872
verification=ok
```

The verifier takes its expected statement from the command line and runs without the trace file. To rerun it independently from the repository root, use the public instruction words in program order followed by the expected values of `x5` and `x6`.

```sh
cargo run --locked --release --manifest-path proof/Cargo.toml -- \
  --verify build/proof.bin 0x00700293 0xffd28293 0xfff00313 0x00230313 4 1
```

| PC | Instruction word | Assembly | Architectural writeback |
| --- | --- | --- | --- |
| `0x00` | `0x00700293` | `addi x5, x0, 7` | `x5 = 7` |
| `0x04` | `0xffd28293` | `addi x5, x5, -3` | `x5 = 4` |
| `0x08` | `0xfff00313` | `addi x6, x0, -1` | `x6 = 0xffffffff` |
| `0x0c` | `0x00230313` | `addi x6, x6, 2` | `x6 = 1` |

## How the ADDI service works

```mermaid
flowchart LR
    J[Authenticated jobs API] --> Q[(SQLite queue and proofs)]
    Q -->|Claim job| A[SystemVerilog core]
    A -->|Retirements| B[CSV trace]
    B -->|Validated witness| C[Halo2 prover]
    C -->|Proof| E[Native verifier]
    D[Expected program and final x5/x6] -->|Public inputs| E
    E -->|Verified proof| Q
```

The circuit starts with all registers at zero. It constrains `ADDI` instruction decoding, sign extension, addition modulo `2^32`, writes to the selected destination, preservation of all other registers, and the zero value of `x0`. The final values of `x5` and `x6` are bound to the verifier's public inputs.

The CSV parser checks the RTL retirement records, including the PC sequence. In-circuit instruction order follows the four fixed proof steps. The verification result establishes the register transition relation for the public inputs under the proof system's assumptions. A physical-device attestation protocol and smart-contract verifier are future work. [Read the design and trust model](docs/design.md).

## Prove a four-step ADDI program

Give the trace simulator a file containing four `ADDI` words, each written as eight hexadecimal digits. This program exercises wraparound and writes to `x0`. It leaves `x5 = 2` and `x6 = 0xfffffffe`.

```sh
make trace
printf '%s\n' fff00293 00328293 ffc28313 00730013 > build/program.hex
vvp build/tb_trace.vvp +program=build/program.hex > build/custom-trace.csv
cargo run --locked --release --manifest-path proof/Cargo.toml -- \
  build/custom-trace.csv build/custom-proof.bin
cargo run --locked --release --manifest-path proof/Cargo.toml -- \
  --verify build/custom-proof.bin 0xfff00293 0x00328293 0xffc28313 0x00730013 2 4294967294
```

Another execution engine can supply a compatible CSV to the same prover. [Trace ABI](docs/design.md#trace-abi).

## Verification and evidence

| Command | What it runs |
| --- | --- |
| `make vm-check` | Expanded VM circuit tests, Yosys synthesis, RTL differential fuzzing, and independent Halo2 proof verification |
| `make vm-fuzz VM_FUZZ_SEED=0xcafe VM_FUZZ_CASES=1024` | Re-run the differential checks with a selected seed and random case count |
| `make vm-proof-fuzz` | Prove sampled RTL traces and verify them against the Python model's public state |
| `make test-rtl` | Self-checking RTL simulation for signed immediates, wraparound, consecutive writes, `x0`, trap handling, and reset |
| `make test-proof` | Rust tests for valid and malformed witnesses and tampered proof statements |
| `make formal` | Bounded Yosys SAT check of RTL interface behavior against a reference model, with its log in `build/formal.log` |
| `make demo` | Generate a simulation trace, compare it with the canonical fixture, and create and verify a proof in separate processes |
| `make check` | Run the regressions, bounded check, and end-to-end demo |
| `make evidence` | Run the verification workflow and record logs, trace, proof, and a structured summary under `build/evidence/` |
| `make service-test` | Exercise API contracts, job persistence, and worker behavior |
| `make service-smoke` | Submit two programs over HTTP, verify both proofs offline, reject tampered bytes, and confirm a proof survives restart |

## Roadmap

| Planned milestone | Acceptance criteria |
| --- | --- |
| Larger execution windows | Evaluate scalable permutation and register arguments, then measure proof and verification cost as the execution window grows. |
| Broader instruction support | Extend the ISA with more integer operations and integrate the expanded proof profile into the service under a versioned API. |
| Agent-assisted review | Generate changes and test cases from the specification. Require independent review of RTL and circuit changes, deterministic regressions, and recorded solver results before merging generated changes. |
| On-chain verifier research | Select a chain and compatible proof backend. Measure verifier cost on that chain and test rejection of malformed proofs and mismatched public inputs. |

## Repository layout

| Path | Contents |
| --- | --- |
| [`rtl/`](rtl/) | Synthesizable VM and ADDI cores and simulation testbenches |
| [`vm/`](vm/) | Eight-step trace parser, Halo2 execution and memory circuits, and prover/verifier CLI |
| [`proof/`](proof/) | Four-step ADDI trace parser, circuit, and CLI |
| [`formal/`](formal/) | Bounded reference model and Yosys SAT script |
| [`service/zkservice/`](service/zkservice/) | Authenticated HTTP API, durable queue, worker, and retention command |
| [`service/zkclient/`](service/zkclient/) | Standard-library Python SDK and operator CLI |
| [`Dockerfile`](Dockerfile) | Non-root service image built from locked Python and Rust dependencies |
| [`fixtures/`](fixtures/) | Canonical program and simulation traces for both profiles |
| [`docs/vm.md`](docs/vm.md) | Expanded VM execution, memory constraints, and trace contract |
| [`docs/design.md`](docs/design.md) | Legacy ADDI interface, trace contract, and trust model |

Licensed under the [MIT License](LICENSE).
