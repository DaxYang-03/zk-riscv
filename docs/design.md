# Prototype design

## RTL execution boundary

`zk_rv32i_addi` consumes a stream of 32-bit instruction words. The caller presents a word for the PC reported by `instr_pc`. The core accepts it on a rising edge of `clk` when both `instr_valid` and `instr_ready` are high. `rst_n` is an asynchronous active-low reset. Reset clears the PC and registers and deasserts readiness.

| Event | Architectural effect | Output |
| --- | --- | --- |
| Accepted `ADDI` | Add a sign-extended 12-bit immediate to the source register, truncate to 32 bits, and advance PC by four. Writes to `x0` are discarded. | `retire_valid` and registered retirement fields |
| Accepted unsupported instruction | Preserve the PC and all registers. The caller can resubmit a word at the same PC. | `trap_valid` |
| Idle | Preserve architectural state. | Neither event asserted |

Consecutive accepted instructions can retire on consecutive clock cycles. The retirement fields are valid on a `retire_valid` cycle. The streamed core can continue beyond four retirements. The trace and proof in this repository use a fixed four-step execution from reset. The instruction encoding and register semantics follow the RV32I `ADDI` definition. [RISC-V unprivileged ISA specification](https://docs.riscv.org/reference/isa/v20240411/unpriv/rv32.html).

## Trace ABI

```csv
step,pc,instruction,rd,rs1,imm,rs1_value,rd_value
0,0,7340691,5,0,7,0,7
1,4,4291986067,5,5,-3,7,4
2,8,4293919507,6,0,-1,0,4294967295
3,12,2294547,6,6,2,4294967295,1
```

| Field | Encoding and meaning |
| --- | --- |
| `step` | Decimal step index from 0 through 3 |
| `pc` | Unsigned 32-bit decimal pre-retirement PC, equal to `4 * step` |
| `instruction` | Unsigned 32-bit decimal instruction word |
| `rd`, `rs1` | Decimal register indices from 0 through 31 |
| `imm` | Signed decimal immediate from −2048 through 2047 |
| `rs1_value` | Unsigned 32-bit decimal source register value before execution |
| `rd_value` | Unsigned 32-bit decimal architectural destination value after execution, zero when `rd` is `x0` |

The host parser checks the header, row count, field types, decoded fields, PC sequence, and arithmetic against an independently reconstructed register file. The resulting register states become the execution witness. The format represents four successful retirements. Trap events have no CSV row.

## Proof statement

The verifier supplies six public field elements in this order.

```text
instruction[0], instruction[1], instruction[2], instruction[3], final_x5, final_x6
```

The witness contains five register snapshots, including the initial zero state. The circuit constrains opcode `0x13` and `funct3 = 0`, bit-decomposes instruction fields, and uses the decoded indices to select the source and destination. A 32-bit result and one-bit carry constrain the wrapped sum. Register transitions enforce writeback to the selected nonzero destination and preservation of all other values. It binds `x5` and `x6` from the fifth snapshot to the final two public inputs. The PC is implicit in the fixed instruction order. Only the host trace parser checks the explicit PC field.

The prover uses `halo2_proofs` 0.3.2 with IPA commitments over the Vesta curve in the Pasta cycle. At circuit parameter `k = 5`, the evaluation domain has 32 rows. The verifier reconstructs the public parameters and verification key from the fixed circuit and consumes the proof and its expected public inputs. The binary proof is a Halo2 Blake2b transcript, and the prover obtains proof randomness from the operating system. [Halo2 Rust API](https://docs.rs/halo2_proofs/0.3.2/halo2_proofs/), [Zcash Halo2 repository](https://github.com/zcash/halo2).

## Trust model and limitations

- **Execution integrity.** A verifier that supplies its own expected inputs can check whether those outputs follow from the four public instructions in this circuit. Security depends on the Halo2 implementation and the completeness of the constraints.
- **Hardware provenance.** The CSV producer may be an RTL simulator or any implementation with an equivalent witness. Device identity, measured bitstreams, and physical execution provenance require a separate attestation protocol.
- **Privacy.** The proof interface accepts the public instructions and final register values. The program and zero reset state are public, so observers can reconstruct the intermediate values for this demo.
- **Formal evidence.** Yosys checks the RTL's public interface against a behavioral model over six solver time points. The first asserts reset, the next four allow symbolic instruction words, validity, and reset, and the sixth observes the final retirement. Timing closure, unbounded equivalence, and circuit soundness require separate evidence. [Bounded check script](../formal/check_addi.ys).
- **Deployment.** Verification currently runs in a native process. Contract verification, proof aggregation, memory semantics, and security review are future work. The implementation has received no production audit.
