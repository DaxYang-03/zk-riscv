use std::{error::Error, fmt};

pub const STEPS: usize = 4;
pub(crate) const REGISTERS: usize = 32;
pub(crate) const OPCODE: u32 = 0x13;

const HEADER: &str = "step,pc,instruction,rd,rs1,imm,rs1_value,rd_value";

/// The verifier supplies these inputs in instruction order, then x5 and x6.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PublicInputs {
    pub instructions: [u32; STEPS],
    pub x5: u32,
    pub x6: u32,
}

#[derive(Debug, Clone, Copy, Default)]
pub(crate) struct Step {
    pub instruction: u32,
    pub rd: u32,
    pub rs1: u32,
    pub imm: i32,
    pub rs1_value: u32,
    pub sum: u32,
    pub carry: bool,
}

#[derive(Debug, Clone)]
pub struct Trace {
    pub(crate) steps: [Step; STEPS],
    pub(crate) states: [[u32; REGISTERS]; STEPS + 1],
}

/// A boundary error with line context suitable for a CLI diagnostic.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TraceError {
    line: usize,
    message: String,
}

impl TraceError {
    fn new(line: usize, message: impl Into<String>) -> Self {
        Self {
            line,
            message: message.into(),
        }
    }
}

impl fmt::Display for TraceError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "trace line {}: {}", self.line, self.message)
    }
}

impl Error for TraceError {}

impl Trace {
    /// Parses the simulator CSV and reproduces every execution step in software.
    pub fn parse(csv: &str) -> Result<Self, TraceError> {
        let mut lines = csv.lines();
        if lines.next() != Some(HEADER) {
            return Err(TraceError::new(1, "unexpected CSV header"));
        }

        let mut steps = [Step::default(); STEPS];
        let mut states = [[0_u32; REGISTERS]; STEPS + 1];
        for (index, step) in steps.iter_mut().enumerate() {
            let line_number = index + 2;
            let line = lines
                .next()
                .ok_or_else(|| TraceError::new(line_number, "missing instruction row"))?;
            let fields: Vec<_> = line.split(',').collect();
            if fields.len() != 8 {
                return Err(TraceError::new(line_number, "expected eight CSV fields"));
            }
            let parse_unsigned = |field: usize, name: &str| {
                fields[field]
                    .parse::<u32>()
                    .map_err(|_| TraceError::new(line_number, format!("invalid {name}")))
            };
            let trace_step = parse_unsigned(0, "step")?;
            let pc = parse_unsigned(1, "pc")?;
            if trace_step != index as u32 || pc != 4 * trace_step {
                return Err(TraceError::new(line_number, "unexpected step or PC"));
            }

            let instruction = parse_unsigned(2, "instruction")?;
            // ADDI has opcode 0010011 and funct3 000.
            if instruction & 0x707f != OPCODE {
                return Err(TraceError::new(
                    line_number,
                    "expected an RV32I ADDI instruction",
                ));
            }
            let rd = (instruction >> 7) & 0x1f;
            let rs1 = (instruction >> 15) & 0x1f;
            let imm = (instruction as i32) >> 20;
            let trace_imm = fields[5]
                .parse::<i32>()
                .map_err(|_| TraceError::new(line_number, "invalid imm"))?;
            if parse_unsigned(3, "rd")? != rd
                || parse_unsigned(4, "rs1")? != rs1
                || trace_imm != imm
            {
                return Err(TraceError::new(
                    line_number,
                    "decoded operands disagree with instruction",
                ));
            }

            let current = states[index];
            let rs1_value = current[rs1 as usize];
            let (sum, carry) = rs1_value.overflowing_add(imm as u32);
            let rd_value = if rd == 0 { 0 } else { sum };
            if parse_unsigned(6, "rs1_value")? != rs1_value
                || parse_unsigned(7, "rd_value")? != rd_value
            {
                return Err(TraceError::new(
                    line_number,
                    "register values disagree with RV32I execution",
                ));
            }
            let mut next = current;
            next[rd as usize] = rd_value;
            states[index + 1] = next;
            *step = Step {
                instruction,
                rd,
                rs1,
                imm,
                rs1_value,
                sum,
                carry,
            };
        }
        if lines.next().is_some() {
            return Err(TraceError::new(
                STEPS + 2,
                "unexpected extra instruction row",
            ));
        }
        Ok(Self { steps, states })
    }

    pub fn public_inputs(&self) -> PublicInputs {
        PublicInputs {
            instructions: self.steps.map(|step| step.instruction),
            x5: self.states[STEPS][5],
            x6: self.states[STEPS][6],
        }
    }
}
