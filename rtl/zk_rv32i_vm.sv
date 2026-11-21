`timescale 1ns/1ps

module zk_rv32i_vm (
    input  logic        clk,
    input  logic        rst_n,
    input  logic        instr_valid,
    input  logic [31:0] instr_word,
    output logic        instr_ready,
    output logic [31:0] instr_pc,

    output logic        retire_valid,
    output logic [31:0] retire_pc,
    output logic [31:0] retire_instr,
    output logic [31:0] retire_next_pc,
    output logic [31:0] retire_rd_value,
    output logic [31:0] retire_mem_addr,
    output logic [31:0] retire_mem_value,

    output logic        trap_valid,
    output logic [2:0]  trap_cause
);
    localparam logic [2:0] TRAP_NONE        = 3'd0;
    localparam logic [2:0] TRAP_FETCH       = 3'd1;
    localparam logic [2:0] TRAP_INSTRUCTION = 3'd2;
    localparam logic [2:0] TRAP_MEMORY      = 3'd3;
    localparam logic [2:0] TRAP_BRANCH      = 3'd4;

    // x0 has no storage. Data memory is separate from the external instruction ROM.
    logic [31:0] registers [1:31];
    logic [31:0] data_words [0:15];
    logic [9:0]  opcode_funct3;
    logic [4:0]  rd, rs1, rs2;
    logic        is_addi, is_lw, is_sw, is_beq, is_bne;
    logic        branch_taken, memory_access, write_rd;
    logic [31:0] rs1_value, rs2_value;
    logic [31:0] i_immediate, s_immediate, b_immediate;
    logic [31:0] sum, branch_target, next_pc, read_word, rd_value;
    logic [2:0]  fault;

    assign instr_ready = rst_n && !trap_valid;
    assign opcode_funct3 = {instr_word[6:0], instr_word[14:12]};
    assign is_addi = opcode_funct3 == {7'h13, 3'h0};
    assign is_lw   = opcode_funct3 == {7'h03, 3'h2};
    assign is_sw   = opcode_funct3 == {7'h23, 3'h2};
    assign is_beq  = opcode_funct3 == {7'h63, 3'h0};
    assign is_bne  = opcode_funct3 == {7'h63, 3'h1};

    assign rd  = instr_word[11:7];
    assign rs1 = instr_word[19:15];
    assign rs2 = instr_word[24:20];
    assign rs1_value = rs1 == 5'd0 ? 32'd0 : registers[rs1];
    assign rs2_value = rs2 == 5'd0 ? 32'd0 : registers[rs2];

    assign i_immediate = {{20{instr_word[31]}}, instr_word[31:20]};
    assign s_immediate = {{20{instr_word[31]}}, instr_word[31:25], instr_word[11:7]};
    assign b_immediate = {{19{instr_word[31]}}, instr_word[31], instr_word[7],
                          instr_word[30:25], instr_word[11:8], 1'b0};
    assign sum = rs1_value + (is_sw ? s_immediate : i_immediate);

    assign branch_taken = (is_beq && rs1_value == rs2_value) ||
                          (is_bne && rs1_value != rs2_value);
    assign branch_target = instr_pc + b_immediate;
    assign next_pc = branch_taken ? branch_target : (instr_pc + 32'd4);
    assign memory_access = is_lw || is_sw;
    assign read_word = data_words[sum[5:2]];
    assign write_rd = (is_addi || is_lw) && rd != 5'd0;
    assign rd_value = is_lw ? read_word : sum;

    always @* begin
        fault = TRAP_NONE;
        if (instr_pc[1:0] != 2'b00 || instr_pc >= 32'd64) begin
            fault = TRAP_FETCH;
        end else if (!(is_addi || memory_access || is_beq || is_bne)) begin
            fault = TRAP_INSTRUCTION;
        end else if (memory_access && (sum[1:0] != 2'b00 || sum >= 32'd64)) begin
            fault = TRAP_MEMORY;
        end else if (branch_taken && (next_pc[1:0] != 2'b00 || next_pc > 32'd64)) begin
            fault = TRAP_BRANCH;
        end
    end

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            instr_pc         <= 32'd0;
            retire_valid     <= 1'b0;
            retire_pc        <= 32'd0;
            retire_instr     <= 32'd0;
            retire_next_pc   <= 32'd0;
            retire_rd_value  <= 32'd0;
            retire_mem_addr  <= 32'd0;
            retire_mem_value <= 32'd0;
            trap_valid       <= 1'b0;
            trap_cause       <= TRAP_NONE;
            for (int index = 1; index < 32; index++) begin
                registers[index] <= 32'd0;
            end
            for (int index = 0; index < 16; index++) begin
                data_words[index] <= 32'd0;
            end
        end else begin
            retire_valid <= 1'b0;
            if (instr_valid && instr_ready) begin
                if (fault != TRAP_NONE) begin
                    trap_valid <= 1'b1;
                    trap_cause <= fault;
                end else begin
                    if (write_rd) begin
                        registers[rd] <= rd_value;
                    end
                    if (is_sw) begin
                        data_words[sum[5:2]] <= rs2_value;
                    end
                    instr_pc         <= next_pc;
                    retire_valid     <= 1'b1;
                    retire_pc        <= instr_pc;
                    retire_instr     <= instr_word;
                    retire_next_pc   <= next_pc;
                    retire_rd_value  <= write_rd ? rd_value : 32'd0;
                    retire_mem_addr  <= memory_access ? sum : 32'd0;
                    retire_mem_value <= is_sw ? rs2_value : (is_lw ? read_word : 32'd0);
                end
            end
        end
    end
endmodule
