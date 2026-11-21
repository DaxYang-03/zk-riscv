`timescale 1ns/1ps

module tb_trace;
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
    logic [31:0]        program_words [0:3];
    logic [31:0]        fixture_results [0:3];
    bit                 check_fixture;
    string              program_path;

    zk_rv32i_addi dut (.*);
    always #5 clk = ~clk;

    task automatic load_program (input string path);
        integer file_handle;
        integer trailing_char;
        string token;
        byte digit;
        logic [31:0] word;

        file_handle = $fopen(path, "r");
        if (file_handle == 0) $fatal(1, "Cannot open program file %s", path);
        for (int step = 0; step < 4; step++) begin
            if ($fscanf(file_handle, "%s", token) != 1 || token.len() != 8) begin
                $fatal(1, "Expected eight hex digits for program word %0d", step);
            end
            for (int index = 0; index < token.len(); index++) begin
                digit = token[index];
                if (!((digit >= "0" && digit <= "9") ||
                      (digit >= "a" && digit <= "f") ||
                      (digit >= "A" && digit <= "F"))) begin
                    $fatal(1, "Invalid hex digit in program word %0d", step);
                end
            end
            if ($sscanf(token, "%h", word) != 1) begin
                $fatal(1, "Cannot decode program word %0d", step);
            end
            program_words[step] = word;
        end
        trailing_char = $fgetc(file_handle);
        while (trailing_char == 32 || (trailing_char >= 9 && trailing_char <= 13)) begin
            trailing_char = $fgetc(file_handle);
        end
        if (trailing_char != -1) begin
            $fatal(1, "Program file must contain exactly four words");
        end
        $fclose(file_handle);
    endtask

    task automatic emit_step (input int step, input logic [31:0] instruction);
        @(negedge clk);
        if (instr_ready !== 1'b1 || instr_pc !== 4 * step) begin
            $fatal(1, "Unexpected input state at trace step %0d", step);
        end
        instr_valid = 1'b1;
        instr_word = instruction;
        @(posedge clk);
        #1;
        if (retire_valid !== 1'b1 || trap_valid !== 1'b0 ||
            retire_pc !== 4 * step || retire_instr !== instruction ||
            (check_fixture && retire_rd_value !== fixture_results[step])) begin
            $fatal(1, "Unexpected retirement at trace step %0d", step);
        end
        $display("%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d", step, retire_pc, retire_instr,
                 retire_rd, retire_rs1, retire_imm, retire_rs1_value, retire_rd_value);
    endtask

    initial begin
        #1_000;
        $fatal(1, "Trace testbench timed out");
    end

    initial begin
        check_fixture = !$value$plusargs("program=%s", program_path);
        if (check_fixture) begin
            if ($test$plusargs("program")) $fatal(1, "Expected +program=<path>");
            program_words[0] = 32'h00700293;
            program_words[1] = 32'hffd28293;
            program_words[2] = 32'hfff00313;
            program_words[3] = 32'h00230313;
            fixture_results[0] = 32'd7;
            fixture_results[1] = 32'd4;
            fixture_results[2] = 32'hffff_ffff;
            fixture_results[3] = 32'd1;
        end else begin
            load_program(program_path);
        end

        repeat (2) @(negedge clk);
        rst_n = 1'b1;

        $display("step,pc,instruction,rd,rs1,imm,rs1_value,rd_value");
        for (int step = 0; step < 4; step++) begin
            emit_step(step, program_words[step]);
        end
        $finish(0);
    end
endmodule
