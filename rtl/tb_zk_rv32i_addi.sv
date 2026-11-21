`timescale 1ns/1ps

module tb_zk_rv32i_addi;
    logic               clk = 1'b0;
    logic               rst_n = 1'b0;
    logic               instr_valid = 1'b0;
    logic [31:0]        instr_word = 32'd0;
    logic               instr_ready;
    logic [31:0]        instr_pc;
    logic               retire_valid;
    logic               trap_valid;
    logic [31:0]        retire_pc;
    logic [31:0]        retire_instr;
    logic [4:0]         retire_rd;
    logic [4:0]         retire_rs1;
    logic signed [31:0] retire_imm;
    logic [31:0]        retire_rs1_value;
    logic [31:0]        retire_rd_value;

    zk_rv32i_addi dut (.*);
    always #5 clk = ~clk;

    task automatic reset_core;
        @(negedge clk);
        rst_n = 1'b0;
        instr_valid = 1'b1;
        instr_word = 32'h00100293; // ADDI x5, x0, 1
        #1;
        if (instr_ready !== 1'b0 || instr_pc !== 32'd0 ||
            retire_valid !== 1'b0 || trap_valid !== 1'b0) begin
            $fatal(1, "Reset did not clear PC and suppress the interface");
        end
        @(posedge clk);
        #1;
        if (instr_pc !== 32'd0 || retire_valid !== 1'b0 || trap_valid !== 1'b0) begin
            $fatal(1, "Instruction input changed state during reset");
        end
        @(negedge clk);
        instr_valid = 1'b0;
        rst_n = 1'b1;
        #1;
        if (instr_ready !== 1'b1) $fatal(1, "Core was not ready after reset");
    endtask

    task automatic check_addi (
        input logic [31:0]        expected_pc,
        input logic [4:0]         rd,
        input logic [4:0]         rs1,
        input logic signed [11:0] imm,
        input logic [31:0]        source,
        input logic [31:0]        result
    );
        logic [31:0]        instruction;
        logic signed [31:0] signed_imm;
        instruction = {imm, rs1, 3'b000, rd, 7'b0010011};
        signed_imm = {{20{imm[11]}}, imm};

        @(negedge clk);
        if (instr_ready !== 1'b1 || instr_pc !== expected_pc) begin
            $fatal(1, "Unexpected input state at PC %08h", expected_pc);
        end
        instr_valid = 1'b1;
        instr_word = instruction;
        @(posedge clk);
        #1;
        if (retire_valid !== 1'b1 || trap_valid !== 1'b0 ||
            instr_pc !== expected_pc + 32'd4 || retire_pc !== expected_pc ||
            retire_instr !== instruction || retire_rd !== rd || retire_rs1 !== rs1 ||
            retire_imm !== signed_imm || retire_rs1_value !== source ||
            retire_rd_value !== result) begin
            $fatal(1, "ADDI mismatch at PC %08h: word=%08h source=%08h result=%08h",
                   expected_pc, instruction, retire_rs1_value, retire_rd_value);
        end
    endtask

    task automatic check_trap (input logic [31:0] pc, input logic [31:0] instruction);
        @(negedge clk);
        instr_valid = 1'b1;
        instr_word = instruction;
        @(posedge clk);
        #1;
        if (trap_valid !== 1'b1 || retire_valid !== 1'b0 || instr_pc !== pc) begin
            $fatal(1, "Unsupported instruction %08h did not trap at PC %08h", instruction, pc);
        end
    endtask

    task automatic check_idle (input logic [31:0] pc);
        @(negedge clk);
        instr_valid = 1'b0;
        instr_word = 32'h00100293; // ADDI x5, x0, 1
        @(posedge clk);
        #1;
        if (retire_valid !== 1'b0 || trap_valid !== 1'b0 || instr_pc !== pc) begin
            $fatal(1, "Architectural state changed without a valid instruction at PC %08h", pc);
        end
    endtask

    initial begin
        #1_000;
        $fatal(1, "Testbench timed out");
    end

    initial begin
        reset_core();
        // Back-to-back retirement, sign extension, modulo 2^32, and x0.
        check_addi(32'd0,  5'd5, 5'd0,  12'sd7,  32'd0,        32'd7);
        check_addi(32'd4,  5'd5, 5'd5, -12'sd3,  32'd7,        32'd4);
        check_addi(32'd8,  5'd6, 5'd0, -12'sd1,  32'd0,        32'hffff_ffff);
        check_addi(32'd12, 5'd6, 5'd6,  12'sd2,  32'hffff_ffff, 32'd1);
        check_addi(32'd16, 5'd0, 5'd5,  12'sd1,  32'd4,        32'd0);
        check_addi(32'd20, 5'd7, 5'd0,  12'sd0,  32'd0,        32'd0);
        check_addi(32'd24, 5'd7, 5'd7,  12'sh800, 32'd0,        32'hffff_f800);
        check_addi(32'd28, 5'd7, 5'd7,  12'sh7ff, 32'hffff_f800, 32'hffff_ffff);

        // Both unsupported encodings name x5 as their destination.
        check_trap(32'd32, 32'h000002b3); // ADD x5, x0, x0
        check_trap(32'd32, 32'h00006293); // ORI x5, x0, 0
        check_idle(32'd32);
        check_addi(32'd32, 5'd5, 5'd5,  12'sd0,  32'd4,        32'd4);
        reset_core();
        check_addi(32'd0,  5'd5, 5'd5,  12'sd0,  32'd0,        32'd0);
        check_addi(32'd4,  5'd6, 5'd6,  12'sd0,  32'd0,        32'd0);
        check_addi(32'd8,  5'd7, 5'd7,  12'sd0,  32'd0,        32'd0);
        check_idle(32'd12);
        $display("PASS: ADDI decode, retire, x0, immediate limits, wraparound, trap, and reset");
        $finish(0);
    end
endmodule
