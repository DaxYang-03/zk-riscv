`timescale 1ns/1ps

module tb_vm_trace;
    localparam integer STEPS = 8;
    localparam integer ROM_WORDS = 16;
    localparam integer STDERR = 32'h80000002;

    logic        clk = 1'b0;
    logic        rst_n = 1'b0;
    logic        instr_valid = 1'b0;
    logic [31:0] instr_word;
    logic        instr_ready;
    logic [31:0] instr_pc;
    logic        retire_valid;
    logic [31:0] retire_pc;
    logic [31:0] retire_instr;
    logic [31:0] retire_next_pc;
    logic [31:0] retire_rd_value;
    logic [31:0] retire_mem_addr;
    logic [31:0] retire_mem_value;
    logic        trap_valid;
    logic [2:0]  trap_cause;
    logic [31:0] rom [0:ROM_WORDS-1];
    string program_path;

    zk_rv32i_vm dut (.*);
    always #5 clk = ~clk;
    assign instr_word = instr_pc < 32'd64 ? rom[instr_pc[5:2]] : 32'd0;

    function automatic logic hex_digit(input byte digit);
        return (digit >= "0" && digit <= "9") ||
               (digit >= "a" && digit <= "f") ||
               (digit >= "A" && digit <= "F");
    endfunction

    task automatic load_program(input string path);
        integer stream;
        integer trailing;
        string token;
        logic [31:0] word;

        stream = $fopen(path, "r");
        if (stream == 0) $fatal(1, "Cannot open ROM file %s", path);
        for (int index = 0; index < ROM_WORDS; index++) begin
            if ($fscanf(stream, "%s", token) != 1 || token.len() != 8) begin
                $fatal(1, "Expected eight hex digits for ROM word %0d", index);
            end
            for (int digit = 0; digit < token.len(); digit++) begin
                if (!hex_digit(token[digit])) $fatal(1, "Invalid hex digit in ROM word %0d", index);
            end
            if ($sscanf(token, "%h", word) != 1) $fatal(1, "Cannot decode ROM word %0d", index);
            rom[index] = word;
        end
        trailing = $fgetc(stream);
        while (trailing == 32 || (trailing >= 9 && trailing <= 13)) begin
            trailing = $fgetc(stream);
        end
        if (trailing != -1) $fatal(1, "ROM file must contain exactly %0d words", ROM_WORDS);
        $fclose(stream);
    endtask

    task automatic step_vm(input integer step);
        logic [31:0] expected_pc;
        logic [31:0] expected_word;
        logic [31:0] previous_registers [1:31];
        logic [31:0] previous_memory [0:15];

        if (instr_ready !== 1'b1) $fatal(1, "Fetch interface not ready at step %0d", step);
        expected_pc = instr_pc;
        expected_word = instr_word;
        for (int index = 1; index < 32; index++) previous_registers[index] = dut.registers[index];
        for (int index = 0; index < 16; index++) previous_memory[index] = dut.data_words[index];
        @(posedge clk);
        #1;
        if (trap_valid) begin
            // Architectural traps emit a machine-readable diagnostic and no retirement.
            if (retire_valid !== 1'b0 || instr_pc !== expected_pc || instr_ready !== 1'b0) begin
                $fatal(1, "A trapping instruction advanced the architectural interface");
            end
            for (int index = 1; index < 32; index++) begin
                if (dut.registers[index] !== previous_registers[index]) begin
                    $fatal(1, "A trapping instruction updated register %0d", index);
                end
            end
            for (int index = 0; index < 16; index++) begin
                if (dut.data_words[index] !== previous_memory[index]) begin
                    $fatal(1, "A trapping instruction updated data word %0d", index);
                end
            end
            @(posedge clk);
            #1;
            if (trap_valid !== 1'b1 || instr_ready !== 1'b0 || instr_pc !== expected_pc ||
                retire_valid !== 1'b0) begin
                $fatal(1, "Trap did not hold until reset");
            end
            $fdisplay(STDERR, "trap,%0d,%0d", instr_pc, trap_cause);
            $finish_and_return(1);
        end
        if (retire_valid !== 1'b1 || retire_pc !== expected_pc ||
            retire_instr !== expected_word || retire_next_pc !== instr_pc) begin
            $fatal(1, "Retirement interface mismatch at step %0d", step);
        end
        $display("%0d,%0d,%0d,%0d,%0d,%0d,%0d", step, retire_pc, retire_instr,
                 retire_next_pc, retire_rd_value, retire_mem_addr, retire_mem_value);
    endtask

    initial begin
        #1000;
        $fatal(1, "VM trace testbench timed out");
    end

    initial begin
        if (!$value$plusargs("program=%s", program_path)) $fatal(1, "Expected +program=<path>");
        load_program(program_path);
        repeat (2) @(negedge clk);
        rst_n = 1'b1;
        instr_valid = 1'b1;
        $display("step,pc,instruction,next_pc,rd_value,mem_addr,mem_value");
        #1;
        for (int step = 0; step < STEPS; step++) begin
            step_vm(step);
        end
        $finish(0);
    end
endmodule
