SHELL := /bin/sh
.DEFAULT_GOAL := help
.DELETE_ON_ERROR:

IVERILOG ?= iverilog
VVP ?= vvp
YOSYS ?= yosys
CARGO ?= cargo
PYTHON ?= python3
BUILD_DIR ?= build
export CARGO_TARGET_DIR ?= $(abspath $(BUILD_DIR))/cargo

RTL := rtl/zk_rv32i_addi.sv
TRACE := $(BUILD_DIR)/trace.csv
PROOF := $(BUILD_DIR)/proof.bin
DEMO_FIXTURE := fixtures/demo_trace.csv
PROOF_MANIFEST := proof/Cargo.toml

.PHONY: help doctor trace demo test test-rtl test-proof formal check evidence
help:
	@printf '%s\n' \
		'make doctor     Check for the required local tools' \
		'make trace      Generate and check the RTL retirement trace' \
		'make demo       Prove and verify the four-instruction RTL run' \
		'make test       Run the RTL and Halo2 regression tests' \
		'make formal     Check bounded RTL equivalence with Yosys SAT' \
		'make check      Run the regressions, bounded checks and demo' \
		'make evidence   Run the checks and collect logs and artifact hashes'

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
