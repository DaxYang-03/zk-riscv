//! Bounded RV32I ADDI execution proofs using Halo2 IPA over Pasta.

mod circuit;
mod trace;

use std::{error::Error, fmt};

use halo2_proofs::{
    pasta::EqAffine,
    plonk::{
        create_proof, keygen_pk, keygen_vk, verify_proof, Error as PlonkError, SingleVerifier,
    },
    poly::commitment::Params,
    transcript::{Blake2bRead, Blake2bWrite, Challenge255},
};
use rand_core::OsRng;

use crate::circuit::{instance, ExecutionCircuit, K};
pub use crate::trace::{PublicInputs, Trace, TraceError, STEPS};

#[derive(Debug)]
pub enum ProofError {
    Halo2(PlonkError),
    TrailingBytes,
}

impl fmt::Display for ProofError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Halo2(error) => write!(f, "Halo2: {error}"),
            Self::TrailingBytes => f.write_str("proof contains trailing bytes"),
        }
    }
}

impl Error for ProofError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Halo2(error) => Some(error),
            Self::TrailingBytes => None,
        }
    }
}

impl From<PlonkError> for ProofError {
    fn from(error: PlonkError) -> Self {
        Self::Halo2(error)
    }
}

/// Produces a randomized Halo2 transcript for a validated trace.
pub fn prove(trace: &Trace) -> Result<Vec<u8>, ProofError> {
    let params = Params::<EqAffine>::new(K);
    let blank = ExecutionCircuit::default();
    let vk = keygen_vk(&params, &blank)?;
    let pk = keygen_pk(&params, vk, &blank)?;
    let public = instance(&trace.public_inputs());
    let circuit = ExecutionCircuit {
        trace: Some(trace.clone()),
    };
    let mut transcript = Blake2bWrite::<_, EqAffine, Challenge255<_>>::init(Vec::new());
    create_proof(
        &params,
        &pk,
        &[circuit],
        &[&[&public]],
        OsRng,
        &mut transcript,
    )?;
    Ok(transcript.finalize())
}

/// Verifies a proof using only the circuit definition and the expected public statement.
pub fn verify(public: &PublicInputs, proof: &[u8]) -> Result<(), ProofError> {
    let params = Params::<EqAffine>::new(K);
    let vk = keygen_vk(&params, &ExecutionCircuit::default())?;
    let public = instance(public);
    let mut remainder = proof;
    let mut transcript = Blake2bRead::<_, EqAffine, Challenge255<_>>::init(&mut remainder);
    verify_proof(
        &params,
        &vk,
        SingleVerifier::new(&params),
        &[&[&public]],
        &mut transcript,
    )?;
    if !remainder.is_empty() {
        return Err(ProofError::TrailingBytes);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use halo2_proofs::dev::MockProver;

    use super::*;

    const DEMO: &str = concat!(
        "step,pc,instruction,rd,rs1,imm,rs1_value,rd_value\n",
        "0,0,7340691,5,0,7,0,7\n",
        "1,4,4291986067,5,5,-3,7,4\n",
        "2,8,4293919507,6,0,-1,0,4294967295\n",
        "3,12,2294547,6,6,2,4294967295,1\n",
    );

    const BOUNDARY: &str = concat!(
        "step,pc,instruction,rd,rs1,imm,rs1_value,rd_value\n",
        "0,0,4293922707,31,0,-1,0,4294967295\n",
        "1,4,4293885971,0,31,-2,4294967295,0\n",
        "2,8,2065043,5,31,1,4294967295,0\n",
        "3,12,2147484435,6,0,-2048,0,4294965248\n",
    );

    fn mock(trace: Trace, public: PublicInputs) -> MockProver<halo2_proofs::pasta::Fp> {
        let circuit = ExecutionCircuit { trace: Some(trace) };
        MockProver::run(K, &circuit, vec![instance(&public).to_vec()]).unwrap()
    }

    #[test]
    fn accepts_demo_and_register_boundaries() {
        let demo = Trace::parse(DEMO).unwrap();
        assert_eq!(
            demo.public_inputs(),
            PublicInputs {
                instructions: [0x00700293, 0xffd28293, 0xfff00313, 0x00230313],
                x5: 4,
                x6: 1,
            }
        );
        mock(demo.clone(), demo.public_inputs()).assert_satisfied();

        let boundary = Trace::parse(BOUNDARY).unwrap();
        assert_eq!(boundary.public_inputs().x5, 0);
        assert_eq!(boundary.public_inputs().x6, 0xfffff800);
        mock(boundary.clone(), boundary.public_inputs()).assert_satisfied();
    }

    #[test]
    fn rejects_invalid_trace_rows() {
        let wrong_pc = DEMO.replace("1,4,", "1,8,");
        let wrong_funct3 = DEMO.replace("7340691", "7344787");
        let wrong_writeback = DEMO.replace("5,5,-3,7,4", "5,5,-3,7,5");
        let extra_step = format!("{DEMO}4,16,19,0,0,0,0,0\n");
        for invalid in [&wrong_pc, &wrong_funct3, &wrong_writeback, &extra_step] {
            assert!(Trace::parse(invalid).is_err());
        }
    }

    #[test]
    fn rejects_inconsistent_circuit_witnesses() {
        let trace = Trace::parse(DEMO).unwrap();
        let public = trace.public_inputs();
        let mut opcode = trace.clone();
        opcode.steps[0].instruction ^= 0x1;
        let bad_opcode = opcode.public_inputs();
        assert!(mock(opcode, bad_opcode).verify().is_err());

        let mut alternate_source = trace.clone();
        alternate_source.steps[1].rs1 = 4;
        assert!(mock(alternate_source, public).verify().is_err());

        let mut carry = trace.clone();
        carry.steps[3].carry = false;
        assert!(mock(carry, public).verify().is_err());

        let mut source = trace.clone();
        source.steps[1].rs1_value = 0;
        assert!(mock(source, public).verify().is_err());

        let mut arbitrary_write = trace.clone();
        arbitrary_write.states[1][31] = 99;
        assert!(mock(arbitrary_write, public).verify().is_err());

        let mut x0 = trace.clone();
        x0.states[1][0] = 1;
        assert!(mock(x0, public).verify().is_err());

        let mut initial = trace;
        initial.states[0][31] = 1;
        assert!(mock(initial, public).verify().is_err());
    }

    #[test]
    fn verifies_proof_against_the_expected_statement() {
        let trace = Trace::parse(DEMO).unwrap();
        let public = trace.public_inputs();
        let mut bytes = prove(&trace).unwrap();
        verify(&public, &bytes).unwrap();

        let wrong_output = PublicInputs { x5: 5, ..public };
        assert!(verify(&wrong_output, &bytes).is_err());

        let mut wrong_program = public;
        wrong_program.instructions[0] ^= 1 << 20;
        assert!(verify(&wrong_program, &bytes).is_err());

        bytes.push(0);
        assert!(matches!(
            verify(&public, &bytes),
            Err(ProofError::TrailingBytes)
        ));
    }
}
