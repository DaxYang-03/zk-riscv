# zk-RISC-V

A runnable hardware-to-proof prototype for verifiable RISC-V execution. The demo simulates a synthesizable SystemVerilog core, exports four RV32I `ADDI` retirements, and creates a Halo2 proof against the resulting execution witness. A separate local verifier checks the proof against four expected instruction words and two expected final register values.

**Prototype scope** · one opcode · four proof steps · full 32-register state · native verifier

## Run the demo

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

## How the pieces fit

```mermaid
flowchart LR
    A[SystemVerilog core] -->|Retirements| B[CSV trace]
    B -->|Validated witness| C[Halo2 prover]
    C -->|Proof| E[Native verifier]
    D[Expected program and final x5/x6] -->|Public inputs| E
```

The circuit starts with all registers at zero. It constrains `ADDI` instruction decoding, sign extension, addition modulo `2^32`, writes to the selected destination, preservation of all other registers, and the zero value of `x0`. The final values of `x5` and `x6` are bound to the verifier's public inputs.

The CSV parser checks the RTL retirement records, including the PC sequence. In-circuit instruction order follows the four fixed proof steps. The verification result establishes the register transition relation for the public inputs under the proof system's assumptions. A physical-device attestation protocol and smart-contract verifier are future work. [Read the design and trust model](docs/design.md).

## Prove a custom program

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
| `make test-rtl` | Self-checking RTL simulation for signed immediates, wraparound, consecutive writes, `x0`, trap handling, and reset |
| `make test-proof` | Rust tests for valid and malformed witnesses and tampered proof statements |
| `make formal` | Bounded Yosys SAT check of RTL interface behavior against a reference model, with its log in `build/formal.log` |
| `make demo` | Generate a simulation trace, compare it with the canonical fixture, and create and verify a proof in separate processes |
| `make check` | Run the regressions, bounded check, and end-to-end demo |
| `make evidence` | Run the verification workflow and record logs, trace, proof, and a structured summary under `build/evidence/` |

## Roadmap

| Planned milestone | Acceptance criteria |
| --- | --- |
| Shared execution specification | Derive test cases and circuit inputs for more integer operations. Compare RTL, a reference interpreter, and the proof circuit against the same corpus. |
| Memory and constraints | Specify load and store semantics, including alignment and failure behavior. Add memory consistency constraints and measure their effect on proof size and verification time. |
| Agent-assisted review | Generate changes and test cases from the specification. Require independent review of RTL and circuit changes, deterministic regressions, and recorded solver results before merging generated changes. |
| On-chain verifier research | Select a chain and compatible proof backend. Measure verifier cost on that chain and test rejection of malformed proofs and mismatched public inputs. |

## Repository layout

| Path | Contents |
| --- | --- |
| [`rtl/`](rtl/) | Synthesizable core and self-checking simulation testbenches |
| [`proof/`](proof/) | Trace parser, Halo2 execution circuit, and prover/verifier CLI |
| [`formal/`](formal/) | Bounded reference model and Yosys SAT script |
| [`fixtures/`](fixtures/) | Expected simulation trace for the four-step demo |
| [`docs/design.md`](docs/design.md) | Hardware interface, trace format, proof statement, and trust model |

Licensed under the [MIT License](LICENSE).
