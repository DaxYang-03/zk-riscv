use std::{env, error::Error, fs, io, process::ExitCode};

use zk_riscv_proof::{prove, verify, PublicInputs, Trace};

const USAGE: &str = "Usage:\n  zk-riscv-proof TRACE.csv [PROOF.bin]\n  zk-riscv-proof --verify PROOF.bin WORD0 WORD1 WORD2 WORD3 X5 X6";

fn parse_word(value: &str) -> Result<u32, Box<dyn Error>> {
    if let Some(hex) = value.strip_prefix("0x") {
        return Ok(u32::from_str_radix(hex, 16)?);
    }
    Ok(value.parse()?)
}

fn run(args: &[String]) -> Result<(), Box<dyn Error>> {
    let (public, proof) = match args {
        [flag] if flag == "--help" || flag == "-h" => {
            println!("{USAGE}");
            return Ok(());
        }
        [flag, path, w0, w1, w2, w3, x5, x6] if flag == "--verify" => {
            let public = PublicInputs {
                instructions: [
                    parse_word(w0)?,
                    parse_word(w1)?,
                    parse_word(w2)?,
                    parse_word(w3)?,
                ],
                x5: parse_word(x5)?,
                x6: parse_word(x6)?,
            };
            let proof = fs::read(path)?;
            verify(&public, &proof)?;
            (public, proof)
        }
        [trace_path] | [trace_path, _] if !trace_path.starts_with("--") => {
            let trace = Trace::parse(&fs::read_to_string(trace_path)?)?;
            let public = trace.public_inputs();
            let proof = prove(&trace)?;
            verify(&public, &proof)?;
            if let Some(path) = args.get(1) {
                fs::write(path, &proof)?;
            }
            (public, proof)
        }
        _ => return Err(io::Error::new(io::ErrorKind::InvalidInput, USAGE).into()),
    };

    let [w0, w1, w2, w3] = public.instructions;
    println!("instructions=0x{w0:08x},0x{w1:08x},0x{w2:08x},0x{w3:08x}");
    println!("final_registers=x5:{},x6:{}", public.x5, public.x6);
    println!("proof_bytes={}", proof.len());
    println!("verification=ok");
    Ok(())
}

fn main() -> ExitCode {
    match run(&env::args().skip(1).collect::<Vec<_>>()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{error}");
            ExitCode::FAILURE
        }
    }
}
