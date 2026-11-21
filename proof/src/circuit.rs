use std::array;

use halo2_proofs::{
    circuit::{AssignedCell, Layouter, SimpleFloorPlanner, Value},
    pasta::Fp,
    plonk::{Advice, Circuit, Column, ConstraintSystem, Error, Expression, Instance, Selector},
    poly::Rotation,
};

use crate::trace::{PublicInputs, Trace, OPCODE, REGISTERS, STEPS};

pub(crate) const K: u32 = 5;

const INDEX_BITS: usize = 5;
const IMMEDIATE_BITS: usize = 12;
const WORD_BITS: usize = 32;

type Expr = Expression<Fp>;

#[derive(Clone, Debug)]
pub(crate) struct Config {
    initial: Selector,
    transition: Selector,
    public: Column<Instance>,
    registers: [Column<Advice>; REGISTERS],
    instruction: Column<Advice>,
    rd_bits: [Column<Advice>; INDEX_BITS],
    rs1_bits: [Column<Advice>; INDEX_BITS],
    imm_bits: [Column<Advice>; IMMEDIATE_BITS],
    sum_bits: [Column<Advice>; WORD_BITS],
    source: Column<Advice>,
    sum: Column<Advice>,
    carry: Column<Advice>,
}

#[derive(Clone, Debug, Default)]
pub(crate) struct ExecutionCircuit {
    pub(crate) trace: Option<Trace>,
}

pub(crate) fn instance(public: &PublicInputs) -> [Fp; STEPS + 2] {
    let mut values = [Fp::zero(); STEPS + 2];
    for (value, word) in values.iter_mut().zip(public.instructions) {
        *value = Fp::from(word as u64);
    }
    values[STEPS] = Fp::from(public.x5 as u64);
    values[STEPS + 1] = Fp::from(public.x6 as u64);
    values
}

fn constant(value: u64) -> Expr {
    Expr::Constant(Fp::from(value))
}

fn integer(bits: &[Expr]) -> Expr {
    bits.iter()
        .enumerate()
        .fold(constant(0), |sum, (index, bit)| {
            sum + bit.clone() * Fp::from(1_u64 << index)
        })
}

fn is_index(bits: &[Expr; INDEX_BITS], register: usize) -> Expr {
    bits.iter()
        .enumerate()
        .fold(constant(1), |product, (index, bit)| {
            let factor = if (register >> index) & 1 == 1 {
                bit.clone()
            } else {
                constant(1) - bit.clone()
            };
            product * factor
        })
}

impl Circuit<Fp> for ExecutionCircuit {
    type Config = Config;
    type FloorPlanner = SimpleFloorPlanner;

    fn without_witnesses(&self) -> Self {
        Self::default()
    }

    fn configure(meta: &mut ConstraintSystem<Fp>) -> Config {
        let config = Config {
            initial: meta.selector(),
            transition: meta.selector(),
            public: meta.instance_column(),
            registers: array::from_fn(|_| meta.advice_column()),
            instruction: meta.advice_column(),
            rd_bits: array::from_fn(|_| meta.advice_column()),
            rs1_bits: array::from_fn(|_| meta.advice_column()),
            imm_bits: array::from_fn(|_| meta.advice_column()),
            sum_bits: array::from_fn(|_| meta.advice_column()),
            source: meta.advice_column(),
            sum: meta.advice_column(),
            carry: meta.advice_column(),
        };
        meta.enable_equality(config.public);
        meta.enable_equality(config.instruction);
        meta.enable_equality(config.registers[5]);
        meta.enable_equality(config.registers[6]);

        meta.create_gate("zero initial register file", |meta| {
            let initial = meta.query_selector(config.initial);
            config
                .registers
                .map(|column| initial.clone() * meta.query_advice(column, Rotation::cur()))
        });

        meta.create_gate("decode, add and update RV32I register file", |meta| {
            let transition = meta.query_selector(config.transition);
            let instruction = meta.query_advice(config.instruction, Rotation::cur());
            let rd_bits = config
                .rd_bits
                .map(|column| meta.query_advice(column, Rotation::cur()));
            let rs1_bits = config
                .rs1_bits
                .map(|column| meta.query_advice(column, Rotation::cur()));
            let imm_bits = config
                .imm_bits
                .map(|column| meta.query_advice(column, Rotation::cur()));
            let sum_bits = config
                .sum_bits
                .map(|column| meta.query_advice(column, Rotation::cur()));
            let source = meta.query_advice(config.source, Rotation::cur());
            let sum = meta.query_advice(config.sum, Rotation::cur());
            let carry = meta.query_advice(config.carry, Rotation::cur());

            let mut constraints: Vec<_> = rd_bits
                .iter()
                .chain(&rs1_bits)
                .chain(&imm_bits)
                .chain(&sum_bits)
                .chain(std::iter::once(&carry))
                .map(|bit| bit.clone() * (bit.clone() - constant(1)))
                .collect();

            let decoded = constant(OPCODE as u64)
                + integer(&rd_bits) * Fp::from(1_u64 << 7)
                + integer(&rs1_bits) * Fp::from(1_u64 << 15)
                + integer(&imm_bits) * Fp::from(1_u64 << 20);
            constraints.push(instruction - decoded);

            // Interpret the sign-extended immediate as an unsigned 32-bit addend.
            let sign = imm_bits[IMMEDIATE_BITS - 1].clone();
            let immediate = integer(&imm_bits) + sign * Fp::from((1_u64 << 32) - (1_u64 << 12));
            constraints.push(sum.clone() - integer(&sum_bits));
            constraints
                .push(source.clone() + immediate - sum.clone() - carry * Fp::from(1_u64 << 32));

            let mut read_value = constant(0);
            for (index, column) in config.registers.iter().enumerate() {
                let current = meta.query_advice(*column, Rotation::cur());
                let next = meta.query_advice(*column, Rotation::next());
                read_value = read_value + is_index(&rs1_bits, index) * current.clone();
                if index == 0 {
                    constraints.push(next);
                } else {
                    let expected =
                        current.clone() + is_index(&rd_bits, index) * (sum.clone() - current);
                    constraints.push(next - expected);
                }
            }
            constraints.push(source - read_value);

            constraints
                .into_iter()
                .map(|constraint| transition.clone() * constraint)
                .collect::<Vec<_>>()
        });
        config
    }

    fn synthesize(&self, config: Config, mut layouter: impl Layouter<Fp>) -> Result<(), Error> {
        let public_cells = layouter.assign_region(
            || "four ADDI transitions",
            |mut region| {
                let mut public_cells: Vec<AssignedCell<Fp, Fp>> = Vec::with_capacity(STEPS + 2);
                config.initial.enable(&mut region, 0)?;

                for row in 0..=STEPS {
                    for (register, column) in config.registers.iter().enumerate() {
                        let value = self.trace.as_ref().map(|trace| trace.states[row][register]);
                        let cell = region.assign_advice(
                            || "register state",
                            *column,
                            row,
                            || field_value(value),
                        )?;
                        if row == STEPS && (register == 5 || register == 6) {
                            public_cells.push(cell);
                        }
                    }
                    if row == STEPS {
                        break;
                    }

                    config.transition.enable(&mut region, row)?;
                    let step = self.trace.as_ref().map(|trace| trace.steps[row]);
                    let word = region.assign_advice(
                        || "instruction word",
                        config.instruction,
                        row,
                        || field_value(step.map(|step| step.instruction)),
                    )?;
                    public_cells.push(word);

                    let groups = [
                        ("rd bit", &config.rd_bits[..], step.map(|step| step.rd)),
                        ("rs1 bit", &config.rs1_bits[..], step.map(|step| step.rs1)),
                        (
                            "immediate bit",
                            &config.imm_bits[..],
                            step.map(|step| step.imm as u32),
                        ),
                        ("sum bit", &config.sum_bits[..], step.map(|step| step.sum)),
                    ];
                    for (name, columns, value) in groups {
                        for (bit, column) in columns.iter().enumerate() {
                            let digit = value.map(|value| (value >> bit) & 1);
                            region.assign_advice(|| name, *column, row, || field_value(digit))?;
                        }
                    }

                    let values = [
                        (
                            "source value",
                            config.source,
                            step.map(|step| step.rs1_value),
                        ),
                        ("wrapped sum", config.sum, step.map(|step| step.sum)),
                        (
                            "carry",
                            config.carry,
                            step.map(|step| u32::from(step.carry)),
                        ),
                    ];
                    for (name, column, value) in values {
                        region.assign_advice(|| name, column, row, || field_value(value))?;
                    }
                }
                Ok(public_cells)
            },
        )?;
        for (row, cell) in public_cells.into_iter().enumerate() {
            layouter.constrain_instance(cell.cell(), config.public, row)?;
        }
        Ok(())
    }
}

fn field_value(value: Option<u32>) -> Value<Fp> {
    match value {
        Some(value) => Value::known(Fp::from(value as u64)),
        None => Value::unknown(),
    }
}
