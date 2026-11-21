#!/usr/bin/env python3
"""Compare eight-step RV32I retirements with Python and optionally check real proofs."""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

STEPS = 8
WORDS = 16
MASK32 = (1 << 32) - 1
HEADER = "step,pc,instruction,next_pc,rd_value,mem_addr,mem_value"
NOP = 0x00000013
DECODE = {(0x13, 0): "addi", (0x03, 2): "lw", (0x23, 2): "sw",
          (0x63, 0): "beq", (0x63, 1): "bne"}
TRAP = {"fetch": 1, "instruction": 2, "memory": 3, "branch": 4}


@dataclass
class Run:
    rows: list[tuple[int, ...]]
    trap: tuple[int, int] | None
    coverage: Counter[str]


def signed(value: int, bits: int) -> int:
    sign = 1 << (bits - 1)
    return (value ^ sign) - sign


def run_reference(program: list[int]) -> Run:
    registers = [0] * 32
    memory: dict[int, int] = {}
    stores: Counter[int] = Counter()
    run = Run([], None, Counter())
    pc = 0
    for step in range(STEPS):
        if pc % 4 or not 0 <= pc < 4 * WORDS:
            run.trap = (pc, TRAP["fetch"])
            break

        instruction = program[pc // 4]
        operation = DECODE.get((instruction & 0x7F, instruction >> 12 & 7))
        rd = instruction >> 7 & 31
        left = registers[instruction >> 15 & 31]
        right = registers[instruction >> 20 & 31]
        imm_i = signed(instruction >> 20, 12)
        next_pc = pc + 4
        rd_value = address = memory_value = 0
        if operation == "addi":
            if rd:
                rd_value = (left + imm_i) & MASK32
                registers[rd] = rd_value
        elif operation in ("lw", "sw"):
            imm_s = signed((instruction >> 25) * 32 + rd, 12)
            original_address = left + (imm_i if operation == "lw" else imm_s)
            address = original_address & MASK32
            if address % 4 or address >= 4 * WORDS:
                run.trap = (pc, TRAP["memory"])
                break
            if original_address != address:
                run.coverage["wrapped_address"] += 1
            if operation == "sw":
                memory_value = right
                memory[address] = memory_value
                stores[address] += 1
            else:
                memory_value = memory.get(address, 0)
                if not stores[address]:
                    run.coverage["zero_initialized_load"] += 1
                if stores[address] > 1:
                    run.coverage["load_after_overwrite"] += 1
                if rd:
                    registers[rd] = rd_value = memory_value
                else:
                    run.coverage["load_to_x0"] += 1
        elif operation in ("beq", "bne"):
            unsigned_offset = ((instruction >> 31) * 4096 + (instruction >> 7 & 1) * 2048
                               + (instruction >> 25 & 63) * 32 + (instruction >> 8 & 15) * 2)
            offset = signed(unsigned_offset, 13)
            take = (left == right) if operation == "beq" else (left != right)
            if take:
                next_pc = pc + offset
                if next_pc % 4 or not 0 <= next_pc <= 4 * WORDS:
                    run.trap = (pc, TRAP["branch"])
                    break
            direction = "backward" if offset < 0 else "forward" if offset > 0 else "self"
            outcome = "taken" if take else "untaken"
            run.coverage[f"{operation}_{outcome}_{direction}"] += 1
        else:
            run.trap = (pc, TRAP["instruction"])
            break

        run.coverage[operation] += 1
        run.rows.append((step, pc, instruction, next_pc, rd_value, address, memory_value))
        pc = next_pc

    if run.trap:
        run.coverage[f"trap_{run.trap[1]}"] += 1
    return run


def i_format(opcode: int, funct3: int, rd: int, base: int, imm: int) -> int:
    return (imm & 0xFFF) << 20 | base << 15 | funct3 << 12 | rd << 7 | opcode


def addi(rd: int, base: int, imm: int) -> int:
    return i_format(0x13, 0, rd, base, imm)


def lw(rd: int, base: int, imm: int) -> int:
    return i_format(0x03, 2, rd, base, imm)


def sw(source: int, base: int, imm: int) -> int:
    encoded = imm & 0xFFF
    return encoded >> 5 << 25 | source << 20 | base << 15 | 2 << 12 | (encoded & 31) << 7 | 0x23


def branch(operation: str, left: int, right: int, offset: int) -> int:
    encoded = offset & 0x1FFF
    return ((encoded >> 12) << 31 | (encoded >> 5 & 63) << 25 | right << 20 | left << 15
            | (operation == "bne") << 12 | (encoded >> 1 & 15) << 8 | (encoded >> 11 & 1) << 7 | 0x63)


def padded(words: list[int]) -> list[int]:
    return words + [NOP] * (WORDS - len(words))


def directed_cases() -> dict[str, list[int]]:
    cases = {
        "self_loop": padded([branch("beq", 0, 0, 0)]),
        "taken_misaligned": padded([branch("beq", 0, 0, 2)]),
        "untaken_misaligned": padded([branch("bne", 0, 0, 2)]),
        "negative_target": padded([branch("beq", 0, 0, -4)]),
        "too_high_target": padded([branch("beq", 0, 0, 68)]),
        "target_boundary_then_fetch": padded([branch("beq", 0, 0, 64)]),
        "target_boundary_last_step": padded([NOP] * 7 + [branch("beq", 0, 0, 36)]),
        "unsupported_opcode": padded([0]),
        "unsupported_funct3": padded([0x00001013]),
        "misaligned_load": padded([lw(0, 0, 2)]),
        "misaligned_store": padded([addi(1, 0, 9), sw(1, 0, 2)]),
        "outside_data_memory": padded([lw(5, 0, 64)]),
        "negative_address": padded([sw(0, 0, -4)]),
        "address_wraparound_and_x0": padded([
            addi(28, 0, -4), addi(31, 0, -2048), sw(31, 28, 4), lw(5, 28, 4),
            lw(0, 28, 64), sw(31, 28, 64), addi(0, 31, 2047), lw(6, 0, 0),
        ]),
        "multi_address_overwrite": padded([
            lw(5, 0, 60), addi(31, 0, 2047), sw(31, 0, 60), lw(6, 0, 60),
            addi(31, 31, 1), sw(31, 0, 0), sw(31, 0, 60), lw(5, 0, 60),
        ]),
    }
    for operation in ("beq", "bne"):
        for taken in (False, True):
            for offset in (-4, 8):
                equal = taken == (operation == "beq")
                cases[f"{operation}_{taken}_{offset}"] = padded([
                    addi(1, 0, 7), addi(2, 0, 7 if equal else 8),
                    branch(operation, 1, 2, offset), addi(5, 5, -3),
                ])
    return cases


def random_program(rng: random.Random) -> list[int]:
    regs = [0, 1, 2, 3, 5, 6, 15, 31]
    program = [addi(1, 0, rng.choice((-2048, -3, 1, 2047))), addi(2, 0, rng.randrange(-2, 3))]
    for index in range(len(program), WORDS):
        operation = rng.randrange(7)
        rd, rs1, rs2 = rng.choices(regs, k=3)
        address = 4 * rng.randrange(WORDS)
        if operation < 2:
            program.append(addi(rd, rs1, rng.randrange(-2048, 2048)))
        elif operation == 2:
            program.append(lw(rd, 0, address))
        elif operation == 3:
            program.append(sw(rs2, 0, address))
        else:
            operation_name = "beq" if operation == 4 else "bne"
            offset = 4 * (rng.randrange(WORDS + 1) - index)
            program.append(branch(operation_name, rs1, rs2, offset))
    return program


def check_case(
    vvp: str, image: Path, rom: Path, name: str, program: list[int],
) -> tuple[Run, str]:
    expected = run_reference(program)
    rom_text = "\n".join(f"{word:08x}" for word in program)
    rom.write_text(rom_text + "\n", encoding="ascii")
    observed = subprocess.run(
        [vvp, str(image), f"+program={rom}"], capture_output=True, text=True,
        timeout=5, check=False,
    )
    expected_output = "\n".join([HEADER] + [",".join(map(str, row)) for row in expected.rows]) + "\n"
    expected_error = f"trap,{expected.trap[0]},{expected.trap[1]}\n" if expected.trap else ""
    if (observed.returncode, observed.stdout, observed.stderr) != (
        int(expected.trap is not None), expected_output, expected_error,
    ):
        raise RuntimeError(
            f"{name} did not match the reference model\nROM\n{rom_text}\n"
            f"expected stdout: {expected_output!r}, stderr: {expected_error!r}\n"
            f"observed exit: {observed.returncode}, stdout: {observed.stdout!r}, stderr: {observed.stderr!r}"
        )
    return expected, observed.stdout


def check_proof(prover: Path, rom: Path, name: str, run: Run, trace: str, index: int) -> int:
    trace_file = rom.with_name(f"proof-trace-{index}.csv")
    proof = rom.with_name(f"proof-{index}.bin")
    trace_file.write_text(trace, encoding="ascii")

    # Derive the public claim from the independent model's architectural writebacks.
    registers = [0] * 32
    for _, _, word, _, writeback, _, _ in run.rows:
        if word & 0x7f in (0x03, 0x13):  # Only LW and ADDI write registers.
            registers[word >> 7 & 31] = writeback
    statement = [run.rows[-1][3], registers[5], registers[6]]

    def call(*command: str | Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(prover), *map(str, command)], capture_output=True, text=True,
            timeout=15, check=False,
        )

    generated = call("prove", rom, trace_file, proof)
    if generated.returncode != 0 or not proof.is_file() or proof.stat().st_size == 0:
        raise RuntimeError(f"{name} did not produce a proof: {generated.stderr.strip()}")

    accepted = call("verify", rom, proof, *map(str, statement))
    if accepted.returncode != 0 or "verification=ok" not in accepted.stdout.splitlines():
        raise RuntimeError(f"{name} proof did not verify: {accepted.stderr.strip()}")

    statement[index % 2] ^= 4 if index % 2 == 0 else 1
    rejected = call("verify", rom, proof, *map(str, statement))
    if rejected.returncode != 1 or "verification=ok" in rejected.stdout.splitlines():
        raise RuntimeError(f"{name} did not reject an altered final PC or x5")
    return proof.stat().st_size


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vvp", default="vvp")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--cases", type=int, default=512)
    parser.add_argument("--seed", type=lambda value: int(value, 0), default=0x5EED)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--prover", type=Path, help="path to a compiled zk-riscv-vm binary")
    parser.add_argument(
        "--proof-cases", type=int, default=0,
        help="prove this many random programs that complete and execute both a branch and a memory access",
    )
    args = parser.parse_args()
    if args.cases < 0:
        parser.error("--cases must be nonnegative")
    if not 0 <= args.proof_cases <= args.cases:
        parser.error("--proof-cases must be between zero and --cases")
    if bool(args.prover) != bool(args.proof_cases):
        parser.error("provide --prover and a positive --proof-cases together")
    prover = args.prover.resolve() if args.prover else None

    coverage: Counter[str] = Counter()
    results: Counter[str] = Counter()
    rng = random.Random(args.seed)
    directed = directed_cases()
    proof_sizes: list[int] = []
    with tempfile.TemporaryDirectory(prefix="zkvm-fuzz-") as temporary:
        rom = Path(temporary) / "program.hex"
        for name, program in directed.items():
            run, _ = check_case(args.vvp, args.image, rom, name, program)
            coverage.update(run.coverage)
            results["trapped" if run.trap else "completed"] += 1
        for index in range(args.cases):
            name = f"seed {args.seed:#x}, random case {index}"
            run, trace = check_case(args.vvp, args.image, rom, name, random_program(rng))
            coverage.update(run.coverage)
            results["trapped" if run.trap else "completed"] += 1
            if (
                prover is not None and len(proof_sizes) < args.proof_cases and not run.trap
                and (run.coverage["beq"] or run.coverage["bne"])
                and (run.coverage["lw"] or run.coverage["sw"])
            ):
                proof_sizes.append(check_proof(prover, rom, name, run, trace, len(proof_sizes)))

    if len(proof_sizes) != args.proof_cases:
        raise RuntimeError(f"only {len(proof_sizes)} of {args.proof_cases} qualifying programs were found")

    required = {
        f"{op}_{outcome}_{direction}"
        for op in ("beq", "bne")
        for outcome in ("taken", "untaken")
        for direction in ("forward", "backward")
    } | {f"trap_{code}" for code in TRAP.values()} | {
        "wrapped_address", "zero_initialized_load", "load_after_overwrite", "load_to_x0",
    }
    missing = required - coverage.keys()
    if missing:
        raise RuntimeError(f"missing required differential coverage: {', '.join(sorted(missing))}")
    report_data = {
        "seed": f"{args.seed:#x}", "directed_cases": len(directed), "random_cases": args.cases,
        "outcomes": dict(results), "coverage": dict(coverage),
    }
    if proof_sizes:
        report_data.update(
            proof_cases=len(proof_sizes), tampered_claims_rejected=len(proof_sizes),
            proof_bytes=sorted(set(proof_sizes)),
        )
    report = json.dumps(report_data, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.write_text(report, encoding="ascii")
    print(report, end="")


if __name__ == "__main__":
    main()
