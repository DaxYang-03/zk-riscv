//! Halo2 IPA proofs of bounded RV32I execution with branches and word-addressed RAM.

mod circuit;
mod machine;
mod memory;

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

use crate::circuit::{instance, VmCircuit, K};
pub use crate::machine::{
    parse_program, MachineError, Program, PublicInputs, Trace, ROM_WORDS, STEPS,
};

pub const PROFILE: &str = "rv32i-memory-branch-8/halo2-ipa-pasta/v1";

#[derive(Debug)]
pub enum ProofError {
    Halo2(PlonkError),
    TrailingBytes,
}

impl fmt::Display for ProofError {
    fn fmt(&self, output: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Halo2(error) => write!(output, "Halo2: {error}"),
            Self::TrailingBytes => output.write_str("proof contains trailing bytes"),
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

/// Produce a randomized Halo2 proof for a validated retirement trace.
pub fn prove(trace: &Trace) -> Result<Vec<u8>, ProofError> {
    let params = Params::<EqAffine>::new(K);
    let blank = VmCircuit { trace: None };
    let vk = keygen_vk(&params, &blank)?;
    let pk = keygen_pk(&params, vk, &blank)?;
    let public = instance(&trace.public_inputs());
    let circuit = VmCircuit {
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

/// Verify against the complete public ROM and final PC, x5, and x6 without a trace.
pub fn verify(public: &PublicInputs, proof: &[u8]) -> Result<(), ProofError> {
    let params = Params::<EqAffine>::new(K);
    let vk = keygen_vk(&params, &VmCircuit { trace: None })?;
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
    use super::*;
    use crate::machine::fixtures::{independent_addresses, overwrite};

    #[test]
    fn proves_a_store_load_overwrite_and_both_taken_branch_variants() {
        let trace = Trace::simulate(overwrite()).unwrap();
        let public = trace.public_inputs();
        assert_eq!((public.pc, public.x5, public.x6), (28, 17, 20));
        let proof = prove(&trace).unwrap();
        verify(&public, &proof).unwrap();

        let mut changed_rom = public;
        changed_rom.program[12] ^= 1; // Even unexecuted ROM words are public.
        assert!(verify(&changed_rom, &proof).is_err());

        let changed_pc = PublicInputs { pc: 36, ..public };
        assert!(verify(&changed_pc, &proof).is_err());
        let changed_register = PublicInputs { x6: 17, ..public };
        assert!(verify(&changed_register, &proof).is_err());

        let mut malformed = proof.clone();
        malformed[0] ^= 1;
        assert!(verify(&public, &malformed).is_err());

        let mut trailing = proof;
        trailing.push(0);
        assert!(matches!(
            verify(&public, &trailing),
            Err(ProofError::TrailingBytes)
        ));
    }

    #[test]
    fn independently_proves_zero_init_multiple_addresses_and_untaken_branches() {
        let trace = Trace::simulate(independent_addresses()).unwrap();
        let public = trace.public_inputs();
        assert_eq!((public.pc, public.x5, public.x6), (32, 0, u32::MAX - 3));
        verify(&public, &prove(&trace).unwrap()).unwrap();
    }
}
