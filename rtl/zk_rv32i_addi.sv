`timescale 1ns/1ps

module zk_rv32i_addi (
    input  logic               clk,
    input  logic               rst_n,
    input  logic               instr_valid,
    input  logic [31:0]        instr_word,
    output logic               instr_ready,
    output logic [31:0]        instr_pc,

    output logic               retire_valid,
    output logic               trap_valid,
    output logic [31:0]        retire_pc,
    output logic [31:0]        retire_instr,
    output logic [4:0]         retire_rd,
    output logic [4:0]         retire_rs1,
    output logic signed [31:0] retire_imm,
    output logic [31:0]        retire_rs1_value,
    output logic [31:0]        retire_rd_value
);
    // x0 has no physical storage. Reads return zero and writes are discarded.
    logic [31:0] registers [1:31];
    logic        is_addi;
    logic [4:0]  rd;
    logic [4:0]  rs1;
    logic [31:0] immediate;
    logic [31:0] rs1_value;
    logic [31:0] sum;

    assign instr_ready = rst_n;
    assign is_addi = instr_word[6:0] == 7'b0010011 && instr_word[14:12] == 3'b000;
    assign rd = instr_word[11:7];
    assign rs1 = instr_word[19:15];
    assign immediate = {{20{instr_word[31]}}, instr_word[31:20]};
    assign rs1_value = rs1 == 5'd0 ? 32'd0 : registers[rs1];
    assign sum = rs1_value + immediate;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            instr_pc         <= 32'd0;
            retire_valid     <= 1'b0;
            trap_valid       <= 1'b0;
            retire_pc        <= 32'd0;
            retire_instr     <= 32'd0;
            retire_rd        <= 5'd0;
            retire_rs1       <= 5'd0;
            retire_imm       <= 32'sd0;
            retire_rs1_value <= 32'd0;
            retire_rd_value  <= 32'd0;
            for (int index = 1; index < 32; index++) begin
                registers[index] <= 32'd0;
            end
        end else begin
            retire_valid <= 1'b0;
            trap_valid <= 1'b0;
            if (instr_valid && instr_ready) begin
                if (is_addi) begin
                    if (rd != 5'd0) begin
                        registers[rd] <= sum;
                    end
                    instr_pc         <= instr_pc + 32'd4;
                    retire_valid     <= 1'b1;
                    retire_pc        <= instr_pc;
                    retire_instr     <= instr_word;
                    retire_rd        <= rd;
                    retire_rs1       <= rs1;
                    retire_imm       <= $signed(immediate);
                    retire_rs1_value <= rs1_value;
                    retire_rd_value  <= rd == 5'd0 ? 32'd0 : sum;
                end else begin
                    // Keep architectural state intact so the caller can retry at the same PC.
                    trap_valid <= 1'b1;
                end
            end
        end
    end
endmodule
