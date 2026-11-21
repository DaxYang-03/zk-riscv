//! Exact bounded permutation and read-after-write checks for the RAM event table.

use std::array;

use halo2_proofs::{
    arithmetic::Field,
    circuit::{AssignedCell, Layouter, Value},
    pasta::Fp,
    plonk::{Advice, Column, ConstraintSystem, Error, Expression, Selector},
    poly::Rotation,
};

use crate::machine::{Trace, RAM_WORDS, STEPS};

const FIELDS: usize = 4;
const ADDRESS: usize = 0;
const TIMESTAMP: usize = 1;
const IS_WRITE: usize = 2;
const DATA: usize = 3;
// The largest key is RAM_WORDS * STEPS + (STEPS - 1) = 135, including padding.
const GAP_BITS: usize = 8;

pub(crate) type Chronology = [[AssignedCell<Fp, Fp>; FIELDS]; STEPS];

#[derive(Clone, Copy, Debug, Default)]
struct Event {
    word: u32,
    timestamp: u32,
    is_write: bool,
    value: u32,
}

impl Event {
    fn key(self) -> u32 {
        STEPS as u32 * self.word + self.timestamp
    }

    fn fields(self) -> [u32; FIELDS] {
        [
            self.word,
            self.timestamp,
            u32::from(self.is_write),
            self.value,
        ]
    }
}

#[derive(Clone, Copy, Debug)]
struct SortedRow {
    event: Event,
    before: u32,
    after: u32,
    same_word: bool,
    address_inverse: Fp,
    gap: u32,
    select: [bool; STEPS],
}

fn chronology(trace: &Trace) -> [Event; STEPS] {
    array::from_fn(|index| match trace.steps[index].memory {
        Some(memory) => Event {
            word: memory.address / 4,
            timestamp: index as u32,
            is_write: memory.is_write,
            value: memory.value,
        },
        None => Event {
            word: RAM_WORDS as u32,
            timestamp: index as u32,
            is_write: false,
            value: 0,
        },
    })
}

fn sorted_rows(events: &[Event; STEPS]) -> [SortedRow; STEPS] {
    let mut order = array::from_fn::<_, STEPS, _>(|index| index);
    order.sort_unstable_by_key(|&index| events[index].key());
    rows_for_order(events, &order)
}

fn rows_for_order(events: &[Event; STEPS], order: &[usize; STEPS]) -> [SortedRow; STEPS] {
    let mut previous: Option<SortedRow> = None;
    array::from_fn(|rank| {
        let index = order[rank];
        let event = events[index];
        let same_word = previous.is_some_and(|row| row.event.word == event.word);
        let before = previous.filter(|_| same_word).map_or(0, |row| row.after);
        let address_delta = previous.map_or(Fp::zero(), |row| {
            Fp::from(event.word as u64) - Fp::from(row.event.word as u64)
        });
        let gap = previous.map_or(0, |row| {
            event.key().wrapping_sub(row.event.key()).wrapping_sub(1)
        });
        let row = SortedRow {
            event,
            before,
            after: if event.is_write { event.value } else { before },
            same_word,
            address_inverse: address_delta.invert().unwrap_or(Fp::zero()),
            gap,
            select: array::from_fn(|column| column == index),
        };
        previous = Some(row);
        row
    })
}

fn integer(value: u64) -> Expression<Fp> {
    Expression::Constant(Fp::from(value))
}

#[derive(Clone, Debug)]
pub(crate) struct MemoryConfig {
    first: Selector,
    subsequent: Selector,
    every: Selector,
    chronological: [Column<Advice>; FIELDS],
    sorted: [Column<Advice>; FIELDS],
    choose: [Column<Advice>; STEPS],
    before: Column<Advice>,
    after: Column<Advice>,
    same_word: Column<Advice>,
    address_inverse: Column<Advice>,
    gap: [Column<Advice>; GAP_BITS],
}

impl MemoryConfig {
    pub(crate) fn configure(meta: &mut ConstraintSystem<Fp>) -> Self {
        let columns = Self {
            first: meta.selector(),
            subsequent: meta.selector(),
            every: meta.selector(),
            chronological: array::from_fn(|_| meta.advice_column()),
            sorted: array::from_fn(|_| meta.advice_column()),
            choose: array::from_fn(|_| meta.advice_column()),
            before: meta.advice_column(),
            after: meta.advice_column(),
            same_word: meta.advice_column(),
            address_inverse: meta.advice_column(),
            gap: array::from_fn(|_| meta.advice_column()),
        };
        for column in columns.chronological {
            meta.enable_equality(column);
        }
        columns.permutation_gate(meta);
        columns.memory_gates(meta);
        columns
    }

    fn permutation_gate(&self, meta: &mut ConstraintSystem<Fp>) {
        // This gate is anchored on the first row so it can see all eight rows. Each
        // sorted row selects exactly one chronological row, and each input row is used once.
        meta.create_gate("exact memory event permutation", |meta| {
            let enabled = meta.query_selector(self.first);
            let flags = array::from_fn::<_, STEPS, _>(|row| {
                self.choose
                    .map(|column| meta.query_advice(column, Rotation(row as i32)))
            });
            let chronological = array::from_fn::<_, STEPS, _>(|row| {
                self.chronological
                    .map(|column| meta.query_advice(column, Rotation(row as i32)))
            });
            let mut constraints = Vec::new();

            for (row, selected) in flags.iter().enumerate() {
                for bit in selected {
                    constraints.push(bit.clone() * (bit.clone() - integer(1)));
                }
                let count = selected
                    .iter()
                    .fold(integer(0), |sum, bit| sum + bit.clone());
                constraints.push(count - integer(1));

                for (field, column) in self.sorted.iter().enumerate() {
                    let claimed = meta.query_advice(*column, Rotation(row as i32));
                    let selected_field = selected
                        .iter()
                        .zip(&chronological)
                        .map(|(bit, input)| bit.clone() * input[field].clone())
                        .fold(integer(0), |sum, value| sum + value);
                    constraints.push(claimed - selected_field);
                }
            }
            for original in 0..STEPS {
                let use_count = flags
                    .iter()
                    .fold(integer(0), |sum, row| sum + row[original].clone());
                constraints.push(use_count - integer(1));
            }

            constraints
                .into_iter()
                .map(|expression| enabled.clone() * expression)
                .collect::<Vec<_>>()
        });
    }

    fn memory_gates(&self, meta: &mut ConstraintSystem<Fp>) {
        meta.create_gate("first RAM group starts at zero", |meta| {
            let enabled = meta.query_selector(self.first);
            let before = meta.query_advice(self.before, Rotation::cur());
            vec![enabled * before]
        });

        meta.create_gate("strict memory order and latest value continuity", |meta| {
            let enabled = meta.query_selector(self.subsequent);
            let address = meta.query_advice(self.sorted[ADDRESS], Rotation::cur());
            let previous_address = meta.query_advice(self.sorted[ADDRESS], Rotation::prev());
            let timestamp = meta.query_advice(self.sorted[TIMESTAMP], Rotation::cur());
            let previous_timestamp = meta.query_advice(self.sorted[TIMESTAMP], Rotation::prev());
            let same = meta.query_advice(self.same_word, Rotation::cur());
            let inverse = meta.query_advice(self.address_inverse, Rotation::cur());
            let before = meta.query_advice(self.before, Rotation::cur());
            let previous_after = meta.query_advice(self.after, Rotation::prev());
            let address_delta = address - previous_address;
            let key_delta =
                address_delta.clone() * integer(STEPS as u64) + timestamp - previous_timestamp;
            let mut constraints = Vec::new();
            let mut gap = integer(0);
            for (index, column) in self.gap.iter().enumerate() {
                let bit = meta.query_advice(*column, Rotation::cur());
                constraints.push(bit.clone() * (bit.clone() - integer(1)));
                gap = gap + bit * integer(1 << index);
            }

            // The two zero-test equations make same = 1 iff the address is unchanged.
            // A new address implies a zero initial value, including the padding group.
            constraints.extend([
                key_delta - integer(1) - gap,
                address_delta.clone() * inverse + same.clone() - integer(1),
                address_delta * same.clone(),
                before - same * previous_after,
            ]);
            constraints
                .into_iter()
                .map(|expression| enabled.clone() * expression)
                .collect::<Vec<_>>()
        });

        meta.create_gate("RAM read and write semantics", |meta| {
            let enabled = meta.query_selector(self.every);
            let is_write = meta.query_advice(self.sorted[IS_WRITE], Rotation::cur());
            let value = meta.query_advice(self.sorted[DATA], Rotation::cur());
            let before = meta.query_advice(self.before, Rotation::cur());
            let after = meta.query_advice(self.after, Rotation::cur());
            let is_read = integer(1) - is_write.clone();

            vec![
                enabled.clone() * is_write.clone() * (is_write.clone() - integer(1)),
                enabled.clone() * is_read.clone() * (value.clone() - before.clone()),
                enabled * (after - (is_read * before + is_write * value)),
            ]
        });
    }

    pub(crate) fn assign(
        &self,
        layouter: &mut impl Layouter<Fp>,
        inputs: &Chronology,
        trace: Option<&Trace>,
    ) -> Result<(), Error> {
        let witness = trace.map(|trace| sorted_rows(&chronology(trace)));
        self.assign_with_witness(layouter, inputs, witness.as_ref())
    }

    fn assign_with_witness(
        &self,
        layouter: &mut impl Layouter<Fp>,
        inputs: &Chronology,
        witness: Option<&[SortedRow; STEPS]>,
    ) -> Result<(), Error> {
        layouter.assign_region(
            || "sort RAM events and verify memory contents",
            |mut region| {
                self.first.enable(&mut region, 0)?;
                for (rank, input) in inputs.iter().enumerate() {
                    self.every.enable(&mut region, rank)?;
                    if rank != 0 {
                        self.subsequent.enable(&mut region, rank)?;
                    }
                    let sorted = witness.map(|rows| rows[rank]);
                    for (field, chronological_cell) in input.iter().enumerate() {
                        chronological_cell.copy_advice(
                            || "execution memory event",
                            &mut region,
                            self.chronological[field],
                            rank,
                        )?;
                        region.assign_advice(
                            || "sorted memory event",
                            self.sorted[field],
                            rank,
                            || u32_value(sorted.map(|row| row.event.fields()[field])),
                        )?;
                    }
                    for (index, column) in self.choose.iter().enumerate() {
                        region.assign_advice(
                            || "memory permutation choice",
                            *column,
                            rank,
                            || u32_value(sorted.map(|row| u32::from(row.select[index]))),
                        )?;
                    }
                    region.assign_advice(
                        || "memory before access",
                        self.before,
                        rank,
                        || u32_value(sorted.map(|row| row.before)),
                    )?;
                    region.assign_advice(
                        || "memory after access",
                        self.after,
                        rank,
                        || u32_value(sorted.map(|row| row.after)),
                    )?;
                    region.assign_advice(
                        || "same address as previous event",
                        self.same_word,
                        rank,
                        || u32_value(sorted.map(|row| u32::from(row.same_word))),
                    )?;
                    region.assign_advice(
                        || "address difference inverse",
                        self.address_inverse,
                        rank,
                        || known(sorted.map(|row| row.address_inverse)),
                    )?;
                    for (index, column) in self.gap.iter().enumerate() {
                        region.assign_advice(
                            || "sorted key gap bit",
                            *column,
                            rank,
                            || u32_value(sorted.map(|row| (row.gap >> index) & 1)),
                        )?;
                    }
                }
                Ok(())
            },
        )
    }
}

fn known<T>(value: Option<T>) -> Value<T> {
    value.map_or_else(Value::unknown, Value::known)
}

fn u32_value(value: Option<u32>) -> Value<Fp> {
    known(value.map(|value| Fp::from(value as u64)))
}

#[cfg(test)]
mod tests {
    use halo2_proofs::{circuit::SimpleFloorPlanner, dev::MockProver, plonk::Circuit};

    use super::*;
    use crate::machine::fixtures::{independent_addresses, overwrite};

    #[derive(Clone, Debug, Default)]
    struct MemoryOnly {
        events: Option<[Event; STEPS]>,
        sorted: Option<[SortedRow; STEPS]>,
    }

    impl Circuit<Fp> for MemoryOnly {
        type Config = (MemoryConfig, [Column<Advice>; FIELDS]);
        type FloorPlanner = SimpleFloorPlanner;

        fn without_witnesses(&self) -> Self {
            Self::default()
        }

        fn configure(meta: &mut ConstraintSystem<Fp>) -> Self::Config {
            let inputs = array::from_fn(|_| {
                let column = meta.advice_column();
                meta.enable_equality(column);
                column
            });
            (MemoryConfig::configure(meta), inputs)
        }

        fn synthesize(
            &self,
            (memory, inputs): Self::Config,
            mut layouter: impl Layouter<Fp>,
        ) -> Result<(), Error> {
            let chronological = layouter.assign_region(
                || "memory-only input",
                |mut region| {
                    let mut rows = Vec::new();
                    for rank in 0..STEPS {
                        let mut cells = Vec::new();
                        for (i, column) in inputs.iter().enumerate() {
                            cells.push(region.assign_advice(
                                || "input field",
                                *column,
                                rank,
                                || u32_value(self.events.map(|rows| rows[rank].fields()[i])),
                            )?);
                        }
                        rows.push(cells.try_into().unwrap());
                    }
                    Ok(Chronology::try_from(rows).unwrap())
                },
            )?;
            memory.assign_with_witness(&mut layouter, &chronological, self.sorted.as_ref())
        }
    }

    fn mock(events: [Event; STEPS], rows: [SortedRow; STEPS]) -> MockProver<Fp> {
        let circuit = MemoryOnly {
            events: Some(events),
            sorted: Some(rows),
        };
        MockProver::run(6, &circuit, vec![]).unwrap()
    }

    #[test]
    fn accepts_multiple_addresses_zero_initial_reads_and_overwrites() {
        for program in [overwrite(), independent_addresses()] {
            let trace = Trace::simulate(program).unwrap();
            let events = chronology(&trace);
            mock(events, sorted_rows(&events)).assert_satisfied();
        }
    }

    #[test]
    fn rejects_forged_load_even_when_sorted_and_chronological_values_agree() {
        let trace = Trace::simulate(overwrite()).unwrap();
        let mut events = chronology(&trace);
        events[6].value = 17; // This stale value predates the second store.
        assert!(mock(events, sorted_rows(&events)).verify().is_err());

        let trace = Trace::simulate(independent_addresses()).unwrap();
        let mut events = chronology(&trace);
        events[7].value = u32::MAX - 3; // Borrow a value from a different address.
        assert!(mock(events, sorted_rows(&events)).verify().is_err());
    }

    #[test]
    fn rejects_duplicate_dropped_or_reordered_events() {
        let events = chronology(&Trace::simulate(overwrite()).unwrap());
        let honest = sorted_rows(&events);
        let mut order = honest.map(|row| row.event.timestamp as usize);
        order[1] = order[0];
        assert!(mock(events, rows_for_order(&events, &order))
            .verify()
            .is_err());

        let mut order = honest.map(|row| row.event.timestamp as usize);
        order.swap(0, 1);
        assert!(mock(events, rows_for_order(&events, &order))
            .verify()
            .is_err());
    }

    #[test]
    fn rejects_fake_address_reset_and_alias() {
        let mut events = chronology(&Trace::simulate(overwrite()).unwrap());
        events[2].value = 0;
        let mut rows = sorted_rows(&events);
        // Alter both a memory value and the group transition, keeping
        // permutations and local read checks valid.
        rows[1].same_word = false;
        rows[1].before = 0;
        rows[1].after = 0;
        rows[2].before = 0;
        assert!(mock(events, rows).verify().is_err());

        let mut events = chronology(&Trace::simulate(independent_addresses()).unwrap());
        let forged_value = u32::MAX - 3;
        for index in [0, 7] {
            events[index].value = forged_value;
        }
        let mut rows = sorted_rows(&events);
        for row in &mut rows[3..5] {
            row.same_word = true;
            row.before = forged_value;
            row.after = forged_value;
        }
        // Prover cannot claim that address 12 is the same as address zero.
        assert!(mock(events, rows).verify().is_err());
    }
}
