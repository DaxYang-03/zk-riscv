# Bounded RV32I execution

The bounded VM proves eight successful retirements from reset. All 32 registers start at zero, `x0` is hardwired to zero, and the 16 data words are zero initialized. An external instruction ROM supplies an instruction at the address requested by the RTL. Program words and data words occupy separate address spaces. Each has 16 words at byte addresses 0 through 60.

| Instruction | Architectural effect |
| --- | --- |
| `ADDI` | Add a sign-extended 12-bit immediate modulo `2^32` and write `rd`. |
| `LW` | Read a data word at `rs1 + sext(imm12)` modulo `2^32` and write `rd`. |
| `SW` | Write `rs2` to the data word at `rs1 + sext(imm12)` modulo `2^32`. |
| `BEQ` | Branch to `PC + sext(imm13)` when the source registers are equal. |
| `BNE` | Branch to `PC + sext(imm13)` when the source registers differ. |

Default fallthrough advances the PC by four bytes. A taken branch target must be four-byte aligned and between byte addresses 0 and 64 inclusive. The last retirement may leave the PC at 64. Any attempted fetch there traps. An untaken branch with a misaligned encoded target falls through. Memory accesses must be aligned and within the data window. Unsupported instructions, invalid fetches, invalid memory accesses, and invalid taken targets cause a sticky trap in the RTL. A trap has no retirement or architectural writes. The proof statement covers exactly eight retirements, so a trap before the eighth retirement cannot produce a witness for this profile. [RISC-V Unprivileged ISA specification](https://docs.riscv.org/reference/isa/v20240411/unpriv/rv32.html)

## Public statement and execution constraints

The public values are the 16 ROM words in address order, the final PC in bytes, and the final values of `x5` and `x6`. The circuit constrains the initial PC and register file, instruction selection from the public ROM, supported instruction encodings, register reads and writes, and each PC transition. It validates the branch predicate and applies signed branch displacement only when the branch is taken. Arithmetic and effective-address constraints enforce their 32-bit wrapping semantics. Executed instructions are decoded in the circuit. Unreached ROM slots can contain arbitrary 32-bit data.

## Offline memory sorting

The witness contains both the execution-order memory accesses and their offline sorted order. A record carries the word index, retirement time, write bit, and value. Steps without a memory operation use a canonical padding record with word index 16, write bit zero, and value zero. The execution constraints bind each record to the decoded instruction and its operands, with loads consuming the record's value and stores writing the second source register.

An 8 × 8 boolean selection matrix checks that the sorted table is an exact permutation of the execution records. Each row and column has exactly one selected bit, and all four fields of each sorted row must equal the selected source record. Adjacent sorted rows must increase in the packed key `8 × word_index + retirement_index`. The circuit constrains each key difference minus one to an eight-bit unsigned integer, which enforces strict order on this domain. The selection matrix grows quadratically with the number of retirement slots.

For each address group the first prior value is zero. In consecutive rows for the same address the prior value carries forward, unless a store updates it. A load must equal the value carried into its row. These checks bind observed loads to the latest preceding store at the same address, including when different addresses are interleaved in execution order.

## Trace contract

The simulator reads 16 whitespace-separated, eight-digit hexadecimal ROM words from the path provided as `+program=`. On success it writes the header and exactly eight rows of unsigned decimal values.

```text
step,pc,instruction,next_pc,rd_value,mem_addr,mem_value
```

`step` is the zero-based retirement index. The two PC fields and `mem_addr` are byte addresses. `rd_value` is the architectural register write, or zero when the instruction writes no register or targets `x0`. A memory row carries the loaded or stored word in `mem_value`. Both memory fields are zero for other operations. The prover reconstructs the witness after checking the CSV against its own reference execution. The verifier receives only the public values and proof.

## Differential and circuit checks

`make vm-fuzz` runs the RTL against an independent Python interpreter. Directed cases enforce coverage for taken and untaken `BEQ` and `BNE` with forward and backward offsets, taken and untaken misaligned targets, each trap class, a read before any store, address wraparound, an overwrite followed by a read, and a load into `x0`. The randomized cases generate new ROMs from the reported seed and compare complete retirement prefixes and exact trap causes. The testbench verifies that a trapping instruction preserves the PC, register file, and data memory. `make vm-synth` uses Yosys to synthesize and check the VM netlist. The legacy Yosys SAT check applies to the four-step ADDI core.

`make vm-proof-fuzz` takes matched RTL traces through the real Halo2 prover. A separate verifier process checks each proof against the ROM and final state computed by Python. The test also checks rejection under a changed public statement. The default proof sample is 16 programs from a seeded run.

```sh
make vm-check
vvp build/tb_vm_trace.vvp +program=fixtures/vm_program.hex > build/custom-vm.csv
cargo run --locked --release --manifest-path vm/Cargo.toml -- \
  prove fixtures/vm_program.hex build/custom-vm.csv build/custom-vm.bin
cargo run --locked --release --manifest-path vm/Cargo.toml -- \
  verify fixtures/vm_program.hex build/custom-vm.bin 28 17 20
```

The proof establishes the constrained transition relation for the bounded profile under the Halo2 proof system assumptions. Verification runs natively with the public ROM and final outputs. The profile uses a fixed execution length, bounded memory, and a separately supplied public program. The HTTP service exposes the legacy ADDI circuit. [Halo2 proof system](https://github.com/zcash/halo2)
