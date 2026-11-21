use std::array;

use halo2_proofs::{
    arithmetic::Field,
    circuit::{AssignedCell, Layouter, Region, SimpleFloorPlanner, Value},
    pasta::Fp,
    plonk::{
        Advice, Circuit, Column, ConstraintSystem, Error, Expression, Fixed, Instance, Selector,
    },
    poly::Rotation,
};

use crate::{
    machine::{PublicInputs, Trace, VmState, REGISTERS, ROM_WORDS, STEPS},
    memory::MemoryConfig,
};

// A 64-row domain leaves space for the instance, the ROM rotations, and blinding.
pub(crate) const K: u32 = 6;
const WORD_BITS: usize = 32;
const ROM_INDEX_BITS: usize = 4;
const PC_BITS: usize = ROM_INDEX_BITS + 1;
const NO_ACCESS: u64 = 16;
const U32_MODULUS: u64 = 1 << 32;
const OPERATIONS: [(u64, u64); 5] = [(0x13, 0), (0x03, 2), (0x23, 2), (0x63, 0), (0x63, 1)];

type Expr = Expression<Fp>;
type Cell = AssignedCell<Fp, Fp>;

#[derive(Clone, Debug)]
pub(crate) struct Config {
    initial: Selector,
    transition: Selector,
    state: Selector,
    final_state: Selector,
    public: Column<Instance>,
    rom: Column<Advice>,
    pc: Column<Advice>,
    pc_bits: [Column<Advice>; PC_BITS],
    registers: [Column<Advice>; REGISTERS],
    clock: Column<Fixed>,
    instruction: Column<Advice>,
    instruction_bits: [Column<Advice>; WORD_BITS],
    operation: [Column<Advice>; OPERATIONS.len()],
    source: [Column<Advice>; 2],
    equal: Column<Advice>,
    compare_inverse: Column<Advice>,
    sum_bits: [Column<Advice>; WORD_BITS],
    carry: Column<Advice>,
    event: [Column<Advice>; 4],
    memory: MemoryConfig,
}

#[derive(Clone, Debug, Default)]
pub(crate) struct VmCircuit {
    pub(crate) trace: Option<Trace>,
}

pub(crate) fn instance(public: &PublicInputs) -> [Fp; ROM_WORDS + 3] {
    array::from_fn(|index| {
        let word = match index {
            ROM_WORDS => public.pc,
            i if i == ROM_WORDS + 1 => public.x5,
            i if i == ROM_WORDS + 2 => public.x6,
            i => public.program[i],
        };
        Fp::from(u64::from(word))
    })
}

fn constant(value: u64) -> Expr {
    Expr::Constant(Fp::from(value))
}

fn integer(bits: &[Expr]) -> Expr {
    bits.iter().enumerate().fold(constant(0), |sum, (i, bit)| {
        sum + bit.clone() * Fp::from(1_u64 << i)
    })
}

fn boolean(bit: &Expr) -> Expr {
    bit.clone() * (bit.clone() - constant(1))
}

fn select_index(bits: &[Expr], index: usize) -> Expr {
    bits.iter()
        .enumerate()
        .fold(constant(1), |product, (i, bit)| {
            let factor = if (index >> i) & 1 == 1 {
                bit.clone()
            } else {
                constant(1) - bit.clone()
            };
            product * factor
        })
}

impl Config {
    fn configure_state(&self, meta: &mut ConstraintSystem<Fp>) {
        meta.create_gate("zero initial PC and register file", |meta| {
            let initial = meta.query_selector(self.initial);
            std::iter::once(self.pc)
                .chain(self.registers)
                .map(|column| initial.clone() * meta.query_advice(column, Rotation::cur()))
                .collect::<Vec<_>>()
        });

        meta.create_gate("word-aligned bounded PC", |meta| {
            let state = meta.query_selector(self.state);
            let transition = meta.query_selector(self.transition);
            let final_state = meta.query_selector(self.final_state);
            let pc = meta.query_advice(self.pc, Rotation::cur());
            let bits = self
                .pc_bits
                .map(|column| meta.query_advice(column, Rotation::cur()));
            let (index, end) = bits.split_at(ROM_INDEX_BITS);
            let mut constraints: Vec<_> = bits
                .iter()
                .map(|bit| state.clone() * boolean(bit))
                .collect();
            constraints.push(state * (pc - integer(&bits) * Fp::from(4)));
            // Fetched PCs address ROM words 0–15. Final PC may also be byte address 64.
            constraints.push(transition * end[0].clone());
            constraints.push(final_state * end[0].clone() * integer(index));
            constraints
        });

        meta.create_gate("fetch instruction from public ROM", |meta| {
            let initial = meta.query_selector(self.initial);
            let rom: [_; ROM_WORDS] =
                array::from_fn(|index| meta.query_advice(self.rom, Rotation(index as i32)));
            (0..STEPS)
                .map(|row| {
                    let rotation = Rotation(row as i32);
                    let bits = self.pc_bits[..ROM_INDEX_BITS]
                        .iter()
                        .map(|column| meta.query_advice(*column, rotation))
                        .collect::<Vec<_>>();
                    let instruction = meta.query_advice(self.instruction, rotation);
                    let selected = rom.iter().enumerate().fold(constant(0), |sum, (i, word)| {
                        sum + select_index(&bits, i) * word.clone()
                    });
                    initial.clone() * (instruction - selected)
                })
                .collect::<Vec<_>>()
        });
    }

    fn configure_transition(&self, meta: &mut ConstraintSystem<Fp>) {
        meta.create_gate("decode and execute ADDI, LW, SW, BEQ, BNE", |meta| {
            let transition = meta.query_selector(self.transition);
            let instruction = meta.query_advice(self.instruction, Rotation::cur());
            let bits = self
                .instruction_bits
                .map(|column| meta.query_advice(column, Rotation::cur()));
            let op = self
                .operation
                .map(|column| meta.query_advice(column, Rotation::cur()));
            let [addi, load, store, beq, bne] = &op;
            let [left, right] = self
                .source
                .map(|column| meta.query_advice(column, Rotation::cur()));
            let sum_bits = self
                .sum_bits
                .map(|column| meta.query_advice(column, Rotation::cur()));
            let carry = meta.query_advice(self.carry, Rotation::cur());

            let mut constraints: Vec<_> = bits
                .iter()
                .chain(&op)
                .chain(&sum_bits)
                .chain(std::iter::once(&carry))
                .map(boolean)
                .collect();
            constraints.push(instruction - integer(&bits));
            constraints.push(
                op.iter()
                    .cloned()
                    .fold(-constant(1), |sum, flag| sum + flag),
            );
            let (opcode, funct3) = op.iter().zip(OPERATIONS).fold(
                (constant(0), constant(0)),
                |(opcode, funct3), (flag, (opcode_value, funct3_value))| {
                    (
                        opcode + flag.clone() * Fp::from(opcode_value),
                        funct3 + flag.clone() * Fp::from(funct3_value),
                    )
                },
            );
            constraints.push(integer(&bits[..7]) - opcode);
            constraints.push(integer(&bits[12..15]) - funct3);

            // I and S immediates are represented as unsigned, sign-extended RV32 words.
            let sign_extension = bits[31].clone() * Fp::from(U32_MODULUS - (1 << 12));
            let i_imm = integer(&bits[20..32]) + sign_extension.clone();
            let s_imm =
                integer(&bits[7..12]) + integer(&bits[25..32]) * Fp::from(32) + sign_extension;
            let immediate = i_imm.clone() + store.clone() * (s_imm - i_imm);
            let sum = integer(&sum_bits);
            constraints
                .push(left.clone() + immediate - sum.clone() - carry * Fp::from(U32_MODULUS));

            // The two zero-test equations force equal=1 iff the operands match.
            let equal = meta.query_advice(self.equal, Rotation::cur());
            let inverse = meta.query_advice(self.compare_inverse, Rotation::cur());
            let diff = left.clone() - right.clone();
            constraints.push(diff.clone() * equal.clone());
            constraints.push(diff * inverse + equal.clone() - constant(1));
            let taken = beq.clone() * equal.clone() + bne.clone() * (constant(1) - equal);
            let branch_offset = integer(&bits[8..12]) * Fp::from(2)
                + integer(&bits[25..31]) * Fp::from(32)
                + bits[7].clone() * Fp::from(2048)
                - bits[31].clone() * Fp::from(4096);
            let pc = meta.query_advice(self.pc, Rotation::cur());
            let next_pc = meta.query_advice(self.pc, Rotation::next());
            constraints.push(next_pc - pc - constant(4) - taken * (branch_offset - constant(4)));

            // Equality links carry these records to the sorted memory argument.
            let [word_index, timestamp, is_write, memory_value] = self
                .event
                .map(|column| meta.query_advice(column, Rotation::cur()));
            let access = load.clone() + store.clone();
            let inactive = constant(1) - access.clone();
            constraints.push(timestamp - meta.query_fixed(self.clock));
            constraints.push(is_write - store.clone());
            constraints.push(
                word_index
                    - inactive.clone() * Fp::from(NO_ACCESS)
                    - access.clone() * integer(&sum_bits[2..6]),
            );
            constraints.push(access.clone() * integer(&sum_bits[..2]));
            constraints.push(access * integer(&sum_bits[6..]));
            constraints.push(inactive * memory_value.clone());
            constraints.push(store.clone() * (memory_value.clone() - right.clone()));

            let mut left_read = constant(0);
            let mut right_read = constant(0);
            let write = addi.clone() + load.clone();
            let writeback = addi.clone() * sum + load.clone() * memory_value;
            for (index, column) in self.registers.iter().enumerate() {
                let current = meta.query_advice(*column, Rotation::cur());
                let next = meta.query_advice(*column, Rotation::next());
                left_read = left_read + select_index(&bits[15..20], index) * current.clone();
                right_read = right_read + select_index(&bits[20..25], index) * current.clone();
                if index == 0 {
                    constraints.push(next);
                } else {
                    let destination = select_index(&bits[7..12], index);
                    constraints.push(
                        next - current.clone()
                            - destination * (writeback.clone() - write.clone() * current),
                    );
                }
            }
            constraints.push(left - left_read);
            constraints.push(right - right_read);

            constraints
                .into_iter()
                .map(|constraint| transition.clone() * constraint)
                .collect::<Vec<_>>()
        });
    }
}

impl Circuit<Fp> for VmCircuit {
    type Config = Config;
    type FloorPlanner = SimpleFloorPlanner;

    fn without_witnesses(&self) -> Self {
        Self::default()
    }

    fn configure(meta: &mut ConstraintSystem<Fp>) -> Config {
        let config = Config {
            initial: meta.selector(),
            transition: meta.selector(),
            state: meta.selector(),
            final_state: meta.selector(),
            public: meta.instance_column(),
            rom: meta.advice_column(),
            pc: meta.advice_column(),
            pc_bits: array::from_fn(|_| meta.advice_column()),
            registers: array::from_fn(|_| meta.advice_column()),
            clock: meta.fixed_column(),
            instruction: meta.advice_column(),
            instruction_bits: array::from_fn(|_| meta.advice_column()),
            operation: array::from_fn(|_| meta.advice_column()),
            source: array::from_fn(|_| meta.advice_column()),
            equal: meta.advice_column(),
            compare_inverse: meta.advice_column(),
            sum_bits: array::from_fn(|_| meta.advice_column()),
            carry: meta.advice_column(),
            event: array::from_fn(|_| meta.advice_column()),
            memory: MemoryConfig::configure(meta),
        };
        meta.enable_equality(config.public);
        for column in [
            config.rom,
            config.pc,
            config.registers[5],
            config.registers[6],
        ]
        .into_iter()
        .chain(config.event)
        {
            meta.enable_equality(column);
        }
        config.configure_state(meta);
        config.configure_transition(meta);
        config
    }

    fn synthesize(&self, config: Config, mut layouter: impl Layouter<Fp>) -> Result<(), Error> {
        let trace = self.trace.as_ref();
        let (chronological, final_state) = layouter.assign_region(
            || "ROM and execution steps",
            |mut region| {
                config.initial.enable(&mut region, 0)?;
                for index in 0..ROM_WORDS {
                    region.assign_advice_from_instance(
                        || "public ROM word",
                        config.public,
                        index,
                        config.rom,
                        index,
                    )?;
                }
                let mut events = Vec::with_capacity(STEPS);
                for row in 0..STEPS {
                    config.assign_state(&mut region, row, trace.map(|trace| &trace.states[row]))?;
                    events.push(config.assign_step(&mut region, row, trace)?);
                }
                config.final_state.enable(&mut region, STEPS)?;
                let final_state = config.assign_state(
                    &mut region,
                    STEPS,
                    trace.map(|trace| &trace.states[STEPS]),
                )?;
                Ok((
                    events.try_into().map_err(|_| Error::Synthesis)?,
                    final_state,
                ))
            },
        )?;
        for (index, cell) in final_state.into_iter().enumerate() {
            layouter.constrain_instance(cell.cell(), config.public, ROM_WORDS + index)?;
        }
        config.memory.assign(&mut layouter, &chronological, trace)
    }
}

impl Config {
    fn assign_state(
        &self,
        region: &mut Region<'_, Fp>,
        row: usize,
        state: Option<&VmState>,
    ) -> Result<[Cell; 3], Error> {
        self.state.enable(region, row)?;
        let pc = assign_word(region, self.pc, row, "byte PC", state.map(|state| state.pc))?;
        assign_bits(
            region,
            &self.pc_bits,
            row,
            "word PC bits",
            state.map(|state| state.pc / 4),
        )?;
        let mut outputs = vec![pc];
        for (index, column) in self.registers.iter().enumerate() {
            let register = state.map(|state| state.registers[index]);
            let cell = assign_word(region, *column, row, "register", register)?;
            if index == 5 || index == 6 {
                outputs.push(cell);
            }
        }
        outputs.try_into().map_err(|_| Error::Synthesis)
    }

    fn assign_step(
        &self,
        region: &mut Region<'_, Fp>,
        row: usize,
        trace: Option<&Trace>,
    ) -> Result<[Cell; 4], Error> {
        self.transition.enable(region, row)?;
        region.assign_fixed(
            || "fixed chronology",
            self.clock,
            row,
            || Value::known(Fp::from(row as u64)),
        )?;
        let instruction = trace.map(|trace| trace.steps[row].instruction);
        assign_word(region, self.instruction, row, "instruction", instruction)?;
        assign_bits(
            region,
            &self.instruction_bits,
            row,
            "instruction bits",
            instruction,
        )?;
        for (column, (opcode, funct3)) in self.operation.iter().zip(OPERATIONS) {
            let flag = instruction.map(|word| {
                u32::from((u64::from(word & 0x7f), u64::from((word >> 12) & 7)) == (opcode, funct3))
            });
            assign_word(region, *column, row, "opcode selector", flag)?;
        }

        let operands = trace.map(|trace| {
            let word = trace.steps[row].instruction;
            let registers = &trace.states[row].registers;
            [
                registers[((word >> 15) & 31) as usize],
                registers[((word >> 20) & 31) as usize],
            ]
        });
        for (index, column) in self.source.iter().enumerate() {
            assign_word(
                region,
                *column,
                row,
                "operands",
                operands.map(|values| values[index]),
            )?;
        }
        assign_word(
            region,
            self.equal,
            row,
            "operand equality",
            operands.map(|[left, right]| u32::from(left == right)),
        )?;
        let inverse = operands.map(|[left, right]| {
            let diff = Fp::from(u64::from(left)) - Fp::from(u64::from(right));
            diff.invert().unwrap_or(Fp::ZERO)
        });
        region.assign_advice(
            || "operand difference inverse",
            self.compare_inverse,
            row,
            || witness(inverse),
        )?;
        let addition = operands.zip(instruction).map(|([left, _], word)| {
            let i_imm = ((word as i32) >> 20) as u32;
            let s_imm = (((word & 0xfe00_0000) | ((word & 0x0000_0f80) << 13)) as i32 >> 20) as u32;
            left.overflowing_add(if word & 0x7f == 0x23 { s_imm } else { i_imm })
        });
        assign_bits(
            region,
            &self.sum_bits,
            row,
            "word sum",
            addition.map(|(sum, _)| sum),
        )?;
        assign_word(
            region,
            self.carry,
            row,
            "carry",
            addition.map(|(_, carry)| u32::from(carry)),
        )?;

        let event = trace.map(|trace| match &trace.steps[row].memory {
            Some(event) => [
                Fp::from(u64::from(event.address)) * Fp::from(4).invert().unwrap(),
                Fp::from(row as u64),
                Fp::from(u64::from(event.is_write)),
                Fp::from(u64::from(event.value)),
            ],
            None => [
                Fp::from(NO_ACCESS),
                Fp::from(row as u64),
                Fp::ZERO,
                Fp::ZERO,
            ],
        });
        let mut cells = Vec::with_capacity(self.event.len());
        for (index, column) in self.event.iter().enumerate() {
            cells.push(region.assign_advice(
                || "chronological memory record",
                *column,
                row,
                || witness(event.map(|fields| fields[index])),
            )?);
        }
        cells.try_into().map_err(|_| Error::Synthesis)
    }
}

fn witness(value: Option<Fp>) -> Value<Fp> {
    match value {
        Some(value) => Value::known(value),
        None => Value::unknown(),
    }
}

fn assign_word(
    region: &mut Region<'_, Fp>,
    column: Column<Advice>,
    row: usize,
    label: &str,
    value: Option<u32>,
) -> Result<Cell, Error> {
    region.assign_advice(
        || label,
        column,
        row,
        || witness(value.map(|word| Fp::from(u64::from(word)))),
    )
}

fn assign_bits(
    region: &mut Region<'_, Fp>,
    columns: &[Column<Advice>],
    row: usize,
    label: &str,
    word: Option<u32>,
) -> Result<(), Error> {
    for (index, column) in columns.iter().enumerate() {
        assign_word(
            region,
            *column,
            row,
            label,
            word.map(|word| (word >> index) & 1),
        )?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use halo2_proofs::dev::MockProver;

    use super::*;
    use crate::machine::fixtures::{independent_addresses, overwrite};

    const NOP: u32 = 0x0000_0013;

    fn mock(trace: Trace, public: &PublicInputs) -> MockProver<Fp> {
        MockProver::run(
            K,
            &VmCircuit { trace: Some(trace) },
            vec![instance(public).to_vec()],
        )
        .expect("the fixed eight-step circuit fits the configured domain")
    }

    #[test]
    fn constrains_backward_control_flow_and_separated_memory_addresses() {
        for program in [overwrite(), independent_addresses()] {
            let trace = Trace::simulate(program).unwrap();
            mock(trace.clone(), &trace.public_inputs()).assert_satisfied();
        }
    }

    #[test]
    fn allows_final_pc_at_end_of_rom_and_a_zero_offset_loop() {
        for (first, expected_pc) in [(0x0200_0263, 64), (0x0000_0063, 0)] {
            let mut program = [NOP; ROM_WORDS];
            program[0] = first;
            let trace = Trace::simulate(program).unwrap();
            assert_eq!(trace.states[STEPS].pc, expected_pc);
            mock(trace.clone(), &trace.public_inputs()).assert_satisfied();
        }
    }

    #[test]
    fn rejects_branch_fallthrough_despite_same_instructions_and_registers() {
        let mut program = [NOP; ROM_WORDS];
        program[0] = 0x0000_0463; // BEQ x0,x0,+8
        let mut trace = Trace::simulate(program).unwrap();
        for state in &mut trace.states[1..] {
            state.pc -= 4;
        }
        // Each falsely addressed cell still contains NOP and the claimed final PC is
        // public. The execution gate must independently enforce the taken branch.
        let public = trace.public_inputs();
        assert!(mock(trace, &public).verify().is_err());
    }

    #[test]
    fn rejects_uncommitted_instruction_and_extra_register_write() {
        let trace = Trace::simulate([NOP; ROM_WORDS]).unwrap();
        let public = trace.public_inputs();
        let mut instruction_tampered = trace.clone();
        instruction_tampered.steps[2].instruction = 0x0010_0013; // ADDI x0,x0,1
        assert!(mock(instruction_tampered, &public).verify().is_err());
        let mut register_tampered = trace;
        for state in &mut register_tampered.states[1..] {
            state.registers[31] = 42;
        }
        assert!(mock(register_tampered, &public).verify().is_err());
    }

    #[test]
    fn rejects_unaligned_event_and_locally_consistent_forged_load() {
        let trace = Trace::simulate(independent_addresses()).unwrap();
        let public = trace.public_inputs();
        let mut address_tampered = trace.clone();
        address_tampered.steps[2].memory.as_mut().unwrap().address = 1;
        assert!(mock(address_tampered, &public).verify().is_err());

        let mut value_tampered = trace;
        // The last LW writes x7. Changing both the result and the writeback preserves
        // local register transitions, but must fail the sorted memory argument.
        value_tampered.steps[STEPS - 1]
            .memory
            .as_mut()
            .unwrap()
            .value = 9;
        value_tampered.states[STEPS].registers[7] = 9;
        assert!(mock(value_tampered, &public).verify().is_err());
    }
}
