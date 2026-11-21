SHELL := /bin/sh
.DEFAULT_GOAL := help
.DELETE_ON_ERROR:

IVERILOG ?= iverilog
VVP ?= vvp
YOSYS ?= yosys
CARGO ?= cargo
PYTHON ?= python3
UV ?= uv
BUILD_DIR ?= build
export CARGO_TARGET_DIR ?= $(abspath $(BUILD_DIR))/cargo

RTL := rtl/zk_rv32i_addi.sv
TRACE := $(BUILD_DIR)/trace.csv
PROOF := $(BUILD_DIR)/proof.bin
DEMO_FIXTURE := fixtures/demo_trace.csv
PROOF_MANIFEST := proof/Cargo.toml
VM_MANIFEST := vm/Cargo.toml
VM_RTL := rtl/zk_rv32i_vm.sv
VM_PROGRAM := fixtures/vm_program.hex
VM_TRACE := $(BUILD_DIR)/vm-trace.csv
VM_PROOF := $(BUILD_DIR)/vm-proof.bin
VM_SIMULATOR := $(BUILD_DIR)/tb_vm_trace.vvp
VM_PROVER := $(abspath $(CARGO_TARGET_DIR))/release/zk-riscv-vm
VM_FUZZ_CASES ?= 512
VM_FUZZ_SEED ?= 0x5eed
VM_PROOF_FUZZ_CASES ?= 16
VM_PROOF_FUZZ_RANDOM_CASES ?= 128
VM_PROOF_FUZZ_SEED ?= 0xbadc0de
SERVICE_PROVER := $(abspath $(CARGO_TARGET_DIR))/release/zk-riscv-proof
SERVICE_SIMULATOR := $(abspath $(BUILD_DIR))/tb_trace.vvp
SERVICE_ENV := ZKR_VVP="$${ZKR_VVP:-$(VVP)}" ZKR_SIMULATOR="$(SERVICE_SIMULATOR)" ZKR_PROVER="$(SERVICE_PROVER)"

.PHONY: help doctor trace demo test test-rtl test-proof formal check evidence
.PHONY: service-build service-test service-smoke serve
.PHONY: vm-trace vm-demo vm-test vm-fuzz vm-proof-fuzz vm-synth vm-check
help:
	@printf '%s\n' \
		'make doctor     Check for the required local tools' \
		'make trace      Generate and check the RTL retirement trace' \
		'make demo       Prove and verify the four-instruction RTL run' \
		'make test       Run the RTL and Halo2 regression tests' \
		'make formal     Check bounded RTL equivalence with Yosys SAT' \
		'make check      Run the regressions, bounded checks and demo' \
		'make evidence   Run the checks and collect logs and artifact hashes' \
		'make vm-demo    Prove and verify eight VM retirements with branches and memory' \
		'make vm-fuzz    Compare RTL retirements and traps against a Python model' \
		'make vm-proof-fuzz  Prove sampled differential traces and verify public outputs' \
		'make vm-check   Run VM proof tests, RTL checks and the end-to-end VM demo' \
		'make serve      Build and launch the authenticated proof API' \
		'make service-test   Run the service tests' \
		'make service-smoke  Generate and verify proofs over HTTP'

doctor:
	@command -v "$(IVERILOG)"
	@command -v "$(VVP)"
	@"$(YOSYS)" -V
	@"$(CARGO)" --version
	@rustc --version

$(BUILD_DIR):
	mkdir -p "$@"

$(BUILD_DIR)/tb_trace.vvp: $(RTL) rtl/tb_trace.sv | $(BUILD_DIR)
	"$(IVERILOG)" -g2012 -Wall -s tb_trace -o "$@" $^

$(BUILD_DIR)/tb_zk_rv32i_addi.vvp: $(RTL) rtl/tb_zk_rv32i_addi.sv | $(BUILD_DIR)
	"$(IVERILOG)" -g2012 -Wall -s tb_zk_rv32i_addi -o "$@" $^

$(TRACE): $(BUILD_DIR)/tb_trace.vvp
	"$(VVP)" "$<" > "$@"

trace: $(TRACE)
	diff -u "$(DEMO_FIXTURE)" "$(TRACE)"

demo: trace
	"$(CARGO)" run --locked --release --manifest-path "$(PROOF_MANIFEST)" -- "$(TRACE)" "$(PROOF)"
	"$(CARGO)" run --locked --release --manifest-path "$(PROOF_MANIFEST)" -- --verify "$(PROOF)" \
		0x00700293 0xffd28293 0xfff00313 0x00230313 4 1

test-rtl: $(BUILD_DIR)/tb_zk_rv32i_addi.vvp
	"$(VVP)" "$<"

test-proof:
	"$(CARGO)" test --locked --release --manifest-path "$(PROOF_MANIFEST)"

test: test-rtl test-proof

formal: | $(BUILD_DIR)
	"$(YOSYS)" -Q -q -l "$(BUILD_DIR)/formal.log" -s formal/check_addi.ys
	@printf '%s\n' 'RTL assertions passed for four instruction slots after reset.'

check: test formal demo

evidence:
	$(PYTHON) scripts/run_evidence.py --build-dir "$(BUILD_DIR)"

$(VM_SIMULATOR): $(VM_RTL) rtl/tb_vm_trace.sv | $(BUILD_DIR)
	"$(IVERILOG)" -g2012 -Wall -s tb_vm_trace -o "$@" $^

vm-trace: $(VM_SIMULATOR)
	"$(VVP)" "$(VM_SIMULATOR)" "+program=$(VM_PROGRAM)" > "$(VM_TRACE)"
	diff -u fixtures/vm_trace.csv "$(VM_TRACE)"

vm-demo: vm-trace
	"$(CARGO)" run --locked --release --manifest-path "$(VM_MANIFEST)" -- prove \
		"$(VM_PROGRAM)" "$(VM_TRACE)" "$(VM_PROOF)"
	"$(CARGO)" run --locked --release --manifest-path "$(VM_MANIFEST)" -- verify \
		"$(VM_PROGRAM)" "$(VM_PROOF)" 28 17 20

vm-test:
	"$(CARGO)" test --locked --release --manifest-path "$(VM_MANIFEST)"

vm-fuzz: $(VM_SIMULATOR)
	$(PYTHON) scripts/vm_fuzz.py --vvp "$(VVP)" --image "$(VM_SIMULATOR)" \
		--seed "$(VM_FUZZ_SEED)" --cases "$(VM_FUZZ_CASES)" --report "$(BUILD_DIR)/vm-fuzz.json"

vm-proof-fuzz: $(VM_SIMULATOR)
	"$(CARGO)" build --locked --release --manifest-path "$(VM_MANIFEST)"
	$(PYTHON) scripts/vm_fuzz.py --vvp "$(VVP)" --image "$(VM_SIMULATOR)" \
		--seed "$(VM_PROOF_FUZZ_SEED)" --cases "$(VM_PROOF_FUZZ_RANDOM_CASES)" \
		--prover "$(VM_PROVER)" --proof-cases "$(VM_PROOF_FUZZ_CASES)" \
		--report "$(BUILD_DIR)/vm-proof-fuzz.json"

vm-synth: | $(BUILD_DIR)
	"$(YOSYS)" -Q -q -l "$(BUILD_DIR)/vm-synth.log" \
		-p 'read_verilog -sv $(VM_RTL); synth -noabc -top zk_rv32i_vm; check -assert; stat'

vm-check: vm-test vm-synth vm-fuzz vm-demo vm-proof-fuzz

service-build: $(BUILD_DIR)/tb_trace.vvp
	"$(CARGO)" build --locked --release --manifest-path "$(PROOF_MANIFEST)"

serve: service-build
	$(SERVICE_ENV) ZKR_DB_PATH="$${ZKR_DB_PATH:-$(abspath $(BUILD_DIR))/service/jobs.sqlite3}" \
		"$(UV)" run --locked --no-dev --project service python -m zkservice

service-test:
	"$(UV)" run --locked --project service pytest service/tests

service-smoke: service-build
	$(SERVICE_ENV) "$(UV)" run --locked --project service python scripts/service_smoke.py \
		--prover "$(SERVICE_PROVER)"
