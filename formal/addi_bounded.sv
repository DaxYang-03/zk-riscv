// The model uses four ordered retirement records to look up source values.
// Its history covers every accepted instruction within this proof bound.
module addi_bounded(
    input wire clk,
    input wire rst_n,
    input wire instr_valid,
    input wire [31:0] instr_word
);
    wire instr_ready;
    wire [31:0] instr_pc;
    wire retire_valid;
    wire trap_valid;
    wire [31:0] retire_pc;
    wire [31:0] retire_instr;
    wire [4:0] retire_rd;
    wire [4:0] retire_rs1;
    wire signed [31:0] retire_imm;
    wire [31:0] retire_rs1_value;
    wire [31:0] retire_rd_value;

    zk_rv32i_addi dut (
        .clk(clk),
        .rst_n(rst_n),
        .instr_valid(instr_valid),
        .instr_word(instr_word),
        .instr_ready(instr_ready),
        .instr_pc(instr_pc),
        .retire_valid(retire_valid),
        .trap_valid(trap_valid),
        .retire_pc(retire_pc),
        .retire_instr(retire_instr),
        .retire_rd(retire_rd),
        .retire_rs1(retire_rs1),
        .retire_imm(retire_imm),
        .retire_rs1_value(retire_rs1_value),
        .retire_rd_value(retire_rd_value)
    );

    wire legal_addi = (instr_word & 32'h0000707f) == 32'h00000013;
    wire [4:0] dst_index = instr_word[11:7];
    wire [4:0] src_index = instr_word[19:15];
    wire signed [31:0] immediate = $signed(instr_word[31:20]);

    reg [3:0] writes;
    reg [4:0] destinations [0:3];
    reg [31:0] values [0:3];
    reg [31:0] source_value;
    integer age;
    always @* begin
        source_value = 32'b0;
        // Age zero is newest. Resolve older writes before newer ones.
        for (age = 3; age >= 0; age = age - 1) begin
            if (writes[age] && destinations[age] == src_index)
                source_value = values[age];
        end
    end
    wire [31:0] destination_value =
        (dst_index == 5'b0) ? 32'b0 : source_value + immediate;

    reg [31:0] expected_pc;
    reg expected_retire;
    reg expected_trap;
    reg [31:0] previous_pc;
    reg [31:0] previous_word;
    reg [31:0] previous_source;
    reg [31:0] previous_destination;
    integer entry;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            writes <= 4'b0;
            for (entry = 0; entry < 4; entry = entry + 1) begin
                destinations[entry] <= 5'b0;
                values[entry] <= 32'b0;
            end
            expected_pc <= 32'b0;
            expected_retire <= 1'b0;
            expected_trap <= 1'b0;
            previous_pc <= 32'b0;
            previous_word <= 32'b0;
            previous_source <= 32'b0;
            previous_destination <= 32'b0;
        end else begin
            expected_retire <= instr_valid && legal_addi;
            expected_trap <= instr_valid && !legal_addi;
            if (instr_valid && legal_addi) begin
                writes <= {writes[2:0], dst_index != 5'b0};
                for (entry = 3; entry > 0; entry = entry - 1) begin
                    destinations[entry] <= destinations[entry - 1];
                    values[entry] <= values[entry - 1];
                end
                destinations[0] <= dst_index;
                values[0] <= destination_value;
                expected_pc <= expected_pc + 32'd4;
                previous_pc <= expected_pc;
                previous_word <= instr_word;
                previous_source <= source_value;
                previous_destination <= destination_value;
            end
        end
    end

    wire signed [31:0] previous_immediate = $signed(previous_word[31:20]);
    always @* begin
        assert(instr_ready == rst_n);
        assert(instr_pc == expected_pc);
        assert(retire_valid == expected_retire);
        assert(trap_valid == expected_trap);
        if (expected_retire) begin
            assert(retire_pc == previous_pc);
            assert(retire_instr == previous_word);
            assert(retire_rs1 == previous_word[19:15]);
            assert(retire_rd == previous_word[11:7]);
            assert(retire_imm == previous_immediate);
            assert(retire_rs1_value == previous_source);
            assert(retire_rd_value == previous_destination);
        end
    end
endmodule
