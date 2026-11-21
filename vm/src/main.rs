use std::{env, error::Error, fs, io, process::ExitCode};
use zk_riscv_vm::{parse_program, prove, verify, PublicInputs, Trace, PROFILE};

const USAGE: &str = "Usage:\n  zk-riscv-vm prove PROGRAM.hex TRACE.csv PROOF.bin\n  zk-riscv-vm verify PROGRAM.hex PROOF.bin FINAL_PC X5 X6";

fn run(args: &[String]) -> Result<(), Box<dyn Error>> {
    let (public, size) = match args {
        [action, rom, csv, output] if action == "prove" => {
            let program = parse_program(&fs::read_to_string(rom)?)?;
            let trace = Trace::parse(program, &fs::read_to_string(csv)?)?;
            let public = trace.public_inputs();
            let proof = prove(&trace)?;
            verify(&public, &proof)?;
            fs::write(output, &proof)?;
            (public, proof.len())
        }
        [action, rom, input, pc, x5, x6] if action == "verify" => {
            let public = PublicInputs {
                program: parse_program(&fs::read_to_string(rom)?)?,
                pc: pc.parse()?,
                x5: x5.parse()?,
                x6: x6.parse()?,
            };
            let proof = fs::read(input)?;
            verify(&public, &proof)?;
            (public, proof.len())
        }
        [arg] if arg == "--help" || arg == "-h" => {
            println!("{USAGE}");
            return Ok(());
        }
        _ => return Err(io::Error::new(io::ErrorKind::InvalidInput, USAGE).into()),
    };

    println!("profile={PROFILE}");
    println!("final_pc={}", public.pc);
    println!("final_registers=x5:{},x6:{}", public.x5, public.x6);
    println!("proof_bytes={size}");
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
