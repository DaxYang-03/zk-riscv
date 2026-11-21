//! Independent software replay for the bounded VM retirement ABI.

use std::{error::Error, fmt};

pub const STEPS: usize = 8;
pub const ROM_WORDS: usize = 16;
pub const RAM_WORDS: usize = 16;
pub const REGISTERS: usize = 32;
pub const TRACE_HEADER: &str = "step,pc,instruction,next_pc,rd_value,mem_addr,mem_value";
const PC_END: u32 = (ROM_WORDS as u32) * 4;
const RAM_END: u32 = (RAM_WORDS as u32) * 4;

pub type Program = [u32; ROM_WORDS];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PublicInputs {
    pub program: Program,
    pub pc: u32,
    pub x5: u32,
    pub x6: u32,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct VmState {
    pub(crate) pc: u32,
    pub(crate) registers: [u32; REGISTERS],
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct MemoryEvent {
    pub(crate) address: u32,
    pub(crate) value: u32,
    pub(crate) is_write: bool,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct Step {
    pub(crate) instruction: u32,
    pub(crate) memory: Option<MemoryEvent>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Trace {
    pub(crate) program: Program,
    pub(crate) states: [VmState; STEPS + 1],
    pub(crate) steps: [Step; STEPS],
}

#[derive(Debug, Eq, PartialEq)]
pub enum MachineError {
    ProgramLength,
    InvalidWord(usize),
    InvalidHeader,
    TraceLength,
    MalformedRow(usize),
    TraceMismatch {
        step: usize,
        field: &'static str,
    },
    Trap {
        step: usize,
        pc: u32,
        reason: &'static str,
    },
}

impl fmt::Display for MachineError {
    fn fmt(&self, output: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ProgramLength => write!(output, "expected exactly {ROM_WORDS} ROM words"),
            Self::InvalidWord(index) => write!(output, "ROM word {index} needs eight hex digits"),
            Self::InvalidHeader => write!(output, "expected trace CSV header {TRACE_HEADER}"),
            Self::TraceLength => write!(output, "expected exactly {STEPS} trace rows"),
            Self::MalformedRow(step) => write!(
                output,
                "trace row {step} needs seven unsigned decimal values"
            ),
            Self::TraceMismatch { step, field } => {
                write!(output, "trace step {step} disagrees with replay in {field}")
            }
            Self::Trap { step, pc, reason } => {
                write!(output, "step {step} traps at PC {pc}: {reason}")
            }
        }
    }
}

impl Error for MachineError {}

pub fn parse_program(text: &str) -> Result<Program, MachineError> {
    let mut words = text.split_ascii_whitespace();
    let mut rom = [0; ROM_WORDS];
    for (index, word) in rom.iter_mut().enumerate() {
        let token = words.next().ok_or(MachineError::ProgramLength)?;
        if token.len() != 8 || !token.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(MachineError::InvalidWord(index));
        }
        *word = u32::from_str_radix(token, 16).map_err(|_| MachineError::InvalidWord(index))?;
    }
    if words.next().is_some() {
        return Err(MachineError::ProgramLength);
    }
    Ok(rom)
}

impl Trace {
    /// Execute a program with zeroed architectural state for exactly eight retirements.
    pub fn simulate(program: Program) -> Result<Self, MachineError> {
        evaluate(program).map(|(trace, _)| trace)
    }

    /// Compare every supplied RTL retirement field against an independent replay.
    pub fn parse(program: Program, csv: &str) -> Result<Self, MachineError> {
        let mut lines = csv.lines();
        if lines.next() != Some(TRACE_HEADER) {
            return Err(MachineError::InvalidHeader);
        }
        let (trace, expected) = evaluate(program)?;
        const FIELDS: [&str; 7] = [
            "step",
            "pc",
            "instruction",
            "next_pc",
            "rd_value",
            "mem_addr",
            "mem_value",
        ];
        for (step, expected) in expected.into_iter().enumerate() {
            let line = lines.next().ok_or(MachineError::TraceLength)?;
            let mut values = line.split(',');
            for (index, expected_value) in expected.into_iter().enumerate() {
                let value = values
                    .next()
                    .filter(|token| {
                        !token.is_empty() && token.bytes().all(|byte| byte.is_ascii_digit())
                    })
                    .and_then(|token| token.parse::<u32>().ok())
                    .ok_or(MachineError::MalformedRow(step))?;
                if value != expected_value {
                    return Err(MachineError::TraceMismatch {
                        step,
                        field: FIELDS[index],
                    });
                }
            }
            if values.next().is_some() {
                return Err(MachineError::MalformedRow(step));
            }
        }
        if lines.next().is_some() {
            return Err(MachineError::TraceLength);
        }
        Ok(trace)
    }

    pub fn public_inputs(&self) -> PublicInputs {
        let final_state = self.states[STEPS];
        PublicInputs {
            program: self.program,
            pc: final_state.pc,
            x5: final_state.registers[5],
            x6: final_state.registers[6],
        }
    }
}

fn sign_extend(value: u32, width: u32) -> i32 {
    ((value << (32 - width)) as i32) >> (32 - width)
}

fn branch_offset(word: u32) -> i32 {
    let bits = ((word >> 31) << 12)
        | (((word >> 7) & 1) << 11)
        | (((word >> 25) & 0x3f) << 5)
        | (((word >> 8) & 0xf) << 1);
    sign_extend(bits, 13)
}

type RetirementRows = [[u32; 7]; STEPS];

fn evaluate(program: Program) -> Result<(Trace, RetirementRows), MachineError> {
    let mut trace = Trace {
        program,
        states: [VmState::default(); STEPS + 1],
        steps: [Step::default(); STEPS],
    };
    let mut rows = [[0; 7]; STEPS];
    let mut ram = [0; RAM_WORDS];

    for (step, row) in rows.iter_mut().enumerate() {
        let current = trace.states[step];
        let pc = current.pc;
        let trap = |reason| MachineError::Trap { step, pc, reason };
        if pc % 4 != 0 || pc >= PC_END {
            return Err(trap("instruction fetch outside the aligned ROM"));
        }
        let word = program[(pc / 4) as usize];
        let rd = ((word >> 7) & 0x1f) as usize;
        let lhs = current.registers[((word >> 15) & 0x1f) as usize];
        let rhs = current.registers[((word >> 20) & 0x1f) as usize];
        let i_imm = sign_extend(word >> 20, 12);
        let mut next = current;
        let mut rd_value = 0;
        let mut event = None;
        next.pc += 4;

        match (word & 0x7f, (word >> 12) & 7) {
            (0x13, 0) => {
                if rd != 0 {
                    rd_value = lhs.wrapping_add(i_imm as u32);
                    next.registers[rd] = rd_value;
                }
            }
            (0x03, 2) | (0x23, 2) => {
                let is_write = word & 0x7f == 0x23;
                let offset = if is_write {
                    sign_extend(((word >> 25) << 5) | ((word >> 7) & 0x1f), 12)
                } else {
                    i_imm
                };
                let address = lhs.wrapping_add(offset as u32);
                if address % 4 != 0 || address >= RAM_END {
                    return Err(trap("data access outside the aligned RAM"));
                }
                let entry = &mut ram[(address / 4) as usize];
                if is_write {
                    *entry = rhs;
                } else if rd != 0 {
                    rd_value = *entry;
                    next.registers[rd] = rd_value;
                }
                event = Some(MemoryEvent {
                    address,
                    value: *entry,
                    is_write,
                });
            }
            (0x63, funct3 @ (0 | 1)) => {
                let taken = (lhs == rhs) == (funct3 == 0);
                if taken {
                    next.pc = pc.wrapping_add(branch_offset(word) as u32);
                    if next.pc % 4 != 0 || next.pc > PC_END {
                        return Err(trap("taken branch target outside the aligned profile"));
                    }
                }
            }
            _ => return Err(trap("unsupported instruction")),
        }

        trace.states[step + 1] = next;
        trace.steps[step] = Step {
            instruction: word,
            memory: event,
        };
        *row = [
            step as u32,
            pc,
            word,
            next.pc,
            rd_value,
            event.map_or(0, |memory| memory.address),
            event.map_or(0, |memory| memory.value),
        ];
    }
    Ok((trace, rows))
}

#[cfg(test)]
pub(crate) mod fixtures {
    use super::*;

    const NOP: u32 = 0x00000013;

    fn i(opcode: u32, funct3: u32, rd: u32, rs1: u32, imm: i32) -> u32 {
        (((imm as u32) & 0xfff) << 20) | (rs1 << 15) | (funct3 << 12) | (rd << 7) | opcode
    }

    fn sw(rs2: u32, rs1: u32, imm: i32) -> u32 {
        let offset = (imm as u32) & 0xfff;
        ((offset >> 5) << 25) | (rs2 << 20) | (rs1 << 15) | (2 << 12) | ((offset & 31) << 7) | 0x23
    }

    fn branch(funct3: u32, rs1: u32, rs2: u32, imm: i32) -> u32 {
        let offset = (imm as u32) & 0x1fff;
        ((offset >> 12) << 31)
            | (((offset >> 5) & 63) << 25)
            | (rs2 << 20)
            | (rs1 << 15)
            | (funct3 << 12)
            | (((offset >> 1) & 15) << 8)
            | (((offset >> 11) & 1) << 7)
            | 0x63
    }

    pub(crate) fn overwrite() -> Program {
        let mut rom = [NOP; ROM_WORDS];
        rom[..9].copy_from_slice(&[
            i(0x13, 0, 1, 0, 17),
            sw(1, 0, 0),
            i(0x03, 2, 5, 0, 0),
            branch(0, 5, 1, 8),
            i(0x13, 0, 6, 0, -1),
            i(0x13, 0, 1, 1, 3),
            sw(1, 0, 0),
            i(0x03, 2, 6, 0, 0),
            branch(1, 6, 5, -4),
        ]);
        rom
    }

    pub(crate) fn independent_addresses() -> Program {
        let mut rom = [NOP; ROM_WORDS];
        rom[..8].copy_from_slice(&[
            i(0x03, 2, 5, 0, 12), // Untouched address reads zero.
            i(0x13, 0, 1, 0, -4),
            sw(1, 1, 4),         // The effective address wraps to zero.
            branch(0, 1, 5, 2),  // Untaken, so the misaligned target is legal.
            i(0x03, 2, 0, 0, 0), // Reads memory but leaves x0 zero.
            branch(1, 5, 0, 2),  // Untaken BNE.
            i(0x03, 2, 6, 0, 0),
            i(0x03, 2, 7, 0, 12), // The other address still reads zero.
        ]);
        rom
    }

    #[test]
    fn replays_signed_wrap_and_untaken_misaligned_branch() {
        let trace = Trace::simulate(independent_addresses()).unwrap();
        assert_eq!(trace.public_inputs().pc, 32);
        assert_eq!(trace.public_inputs().x5, 0);
        assert_eq!(trace.public_inputs().x6, u32::MAX - 3);
        assert_eq!(trace.states[STEPS].registers[0], 0);
        assert_eq!(trace.steps[4].memory.unwrap().value, u32::MAX - 3);
        assert_eq!(trace.steps[7].memory.unwrap().value, 0);
    }

    #[test]
    fn rejects_taken_misalignment_and_invalid_memory_accesses() {
        for word in [branch(0, 0, 0, 2), i(0x03, 2, 1, 0, 64), sw(0, 0, -4), 0] {
            let mut rom = [NOP; ROM_WORDS];
            rom[0] = word;
            assert!(matches!(
                Trace::simulate(rom),
                Err(MachineError::Trap { step: 0, .. })
            ));
        }
    }

    #[test]
    fn compares_every_retirement_field_to_replay() {
        let program = overwrite();
        let (trace, mut rows) = evaluate(program).unwrap();
        let to_csv = |rows: &RetirementRows| {
            let mut csv = String::from(TRACE_HEADER);
            for row in rows {
                csv.push('\n');
                csv.push_str(&row.iter().map(u32::to_string).collect::<Vec<_>>().join(","));
            }
            csv
        };
        assert_eq!(Trace::parse(program, &to_csv(&rows)).unwrap(), trace);
        for column in 0..7 {
            rows[6][column] ^= 1;
            assert!(matches!(
                Trace::parse(program, &to_csv(&rows)),
                Err(MachineError::TraceMismatch { step: 6, .. })
            ));
            rows[6][column] ^= 1;
        }
    }
}
