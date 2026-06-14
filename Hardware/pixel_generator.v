module pixel_generator(
input               out_stream_aclk,
input               s_axi_lite_aclk,
input               axi_resetn,
input               periph_resetn,
output [31:0]       out_stream_tdata,
output [3:0]        out_stream_tkeep,
output              out_stream_tlast,
input               out_stream_tready,
output              out_stream_tvalid,
output [0:0]        out_stream_tuser, 

input [AXI_LITE_ADDR_WIDTH-1:0]     s_axi_lite_araddr,
output          s_axi_lite_arready,
input           s_axi_lite_arvalid,
input [AXI_LITE_ADDR_WIDTH-1:0]     s_axi_lite_awaddr,
output          s_axi_lite_awready,
input           s_axi_lite_awvalid,
input           s_axi_lite_bready,
output [1:0]    s_axi_lite_bresp,
output          s_axi_lite_bvalid,
output [31:0]   s_axi_lite_rdata,
input           s_axi_lite_rready,
output [1:0]    s_axi_lite_rresp,
output          s_axi_lite_rvalid,
input  [31:0]   s_axi_lite_wdata,
output          s_axi_lite_wready,
input           s_axi_lite_wvalid
);

localparam X_SIZE = 640;
localparam Y_SIZE = 480;
parameter  REG_FILE_SIZE = 8;
localparam REG_FILE_AWIDTH = $clog2(REG_FILE_SIZE);
parameter  AXI_LITE_ADDR_WIDTH = 8;

localparam AWAIT_WADD_AND_DATA = 3'b000;
localparam AWAIT_WDATA         = 3'b001;
localparam AWAIT_WADD          = 3'b010;
localparam AWAIT_WRITE         = 3'b100;
localparam AWAIT_RESP          = 3'b101;
localparam AWAIT_RADD          = 2'b00;
localparam AWAIT_FETCH         = 2'b01;
localparam AWAIT_READ          = 2'b10;
localparam AXI_OK              = 2'b00;
localparam AXI_ERR             = 2'b10;

localparam [9:0] SPEED  = 10'd4;

// regfile[1] source 1 Y in upper 16 bits, X in lower 16 bits
wire [9:0] SRC1_X = regfile[1][15:0]; 
wire [8:0] SRC1_Y = regfile[1][31:16];

// regfile[2] source 2 Y in upper 16 bits, X in lower 16 bits
wire [9:0] SRC2_X = regfile[2][15:0];
wire [8:0] SRC2_Y = regfile[2][31:16];

reg [31:0]                          regfile [REG_FILE_SIZE-1:0];
reg [REG_FILE_AWIDTH-1:0]           writeAddr, readAddr;
reg [31:0]                          readData, writeData;
reg [1:0]                           readState  = AWAIT_RADD;
reg [2:0]                           writeState = AWAIT_WADD_AND_DATA;

// AXI-Lite read state machine
always @(posedge s_axi_lite_aclk) begin
    readData <= regfile[readAddr];
    if (!axi_resetn) begin
        readState <= AWAIT_RADD;
    end
    else case (readState)
        AWAIT_RADD: begin
            if (s_axi_lite_arvalid) begin
                readAddr  <= s_axi_lite_araddr[2+:REG_FILE_AWIDTH];
                readState <= AWAIT_FETCH;
            end
        end
        AWAIT_FETCH: begin
            readState <= AWAIT_READ;
        end
        AWAIT_READ: begin
            if (s_axi_lite_rready) begin
                readState <= AWAIT_RADD;
            end
        end
        default: begin
            readState <= AWAIT_RADD;
        end
    endcase
end

assign s_axi_lite_arready = (readState == AWAIT_RADD);
assign s_axi_lite_rresp   = (readAddr < REG_FILE_SIZE) ? AXI_OK : AXI_ERR;
assign s_axi_lite_rvalid  = (readState == AWAIT_READ);
assign s_axi_lite_rdata   = readData;

// AXI-Lite write state machine
always @(posedge s_axi_lite_aclk) begin
    if (!axi_resetn) begin
        writeState <= AWAIT_WADD_AND_DATA;
    end
    else case (writeState)
        AWAIT_WADD_AND_DATA: begin
            case ({s_axi_lite_awvalid, s_axi_lite_wvalid})
                2'b10: begin
                    writeAddr  <= s_axi_lite_awaddr[2+:REG_FILE_AWIDTH];
                    writeState <= AWAIT_WDATA;
                end
                2'b01: begin
                    writeData  <= s_axi_lite_wdata;
                    writeState <= AWAIT_WADD;
                end
                2'b11: begin
                    writeData  <= s_axi_lite_wdata;
                    writeAddr  <= s_axi_lite_awaddr[2+:REG_FILE_AWIDTH];
                    writeState <= AWAIT_WRITE;
                end
                default: begin
                    writeState <= AWAIT_WADD_AND_DATA;
                end
            endcase
        end
        AWAIT_WDATA: begin
            if (s_axi_lite_wvalid) begin
                writeData  <= s_axi_lite_wdata;
                writeState <= AWAIT_WRITE;
            end
        end
        AWAIT_WADD: begin
            if (s_axi_lite_awvalid) begin
                writeAddr  <= s_axi_lite_awaddr[2+:REG_FILE_AWIDTH];
                writeState <= AWAIT_WRITE;
            end
        end
        AWAIT_WRITE: begin
            regfile[writeAddr] <= writeData;
            writeState         <= AWAIT_RESP;
        end
        AWAIT_RESP: begin
            if (s_axi_lite_bready) begin
                writeState <= AWAIT_WADD_AND_DATA;
            end
        end
        default: begin
            writeState <= AWAIT_WADD_AND_DATA;
        end
    endcase
end

assign s_axi_lite_awready = (writeState == AWAIT_WADD_AND_DATA || writeState == AWAIT_WADD);
assign s_axi_lite_wready  = (writeState == AWAIT_WADD_AND_DATA || writeState == AWAIT_WDATA);
assign s_axi_lite_bvalid  = (writeState == AWAIT_RESP);
assign s_axi_lite_bresp   = (writeAddr < REG_FILE_SIZE) ? AXI_OK : AXI_ERR;

// Pixel counter
reg [9:0] x;
reg [8:0] y;

wire first = (x == 0) & (y == 0);
wire lastx = (x == X_SIZE - 1);
wire lasty = (y == Y_SIZE - 1);
wire ready;

always @(posedge out_stream_aclk) begin
    if (!periph_resetn) begin
        for (i = 0; i < CORDIC_LATENCY; i = i+1) begin
            x_dly[i]     <= 0;
            y_dly[i]     <= 0;
            first_dly[i] <= 0;
            lastx_dly[i] <= 0;
            lasty_dly[i] <= 0;
        end
    end else if (pipeline_filled && ready) begin
        for (i = 1; i < CORDIC_LATENCY; i = i+1) begin
            x_dly[i]     <= x_dly[i-1];
            y_dly[i]     <= y_dly[i-1];
            first_dly[i] <= first_dly[i-1];
            lastx_dly[i] <= lastx_dly[i-1];
            lasty_dly[i] <= lasty_dly[i-1];
        end
        x_dly[0]     <= x;
        y_dly[0]     <= y;
        first_dly[0] <= first;
        lastx_dly[0] <= lastx;
        lasty_dly[0] <= lasty;
    end
end


wire valid_int = pipeline_filled;

localparam CORDIC_LATENCY = 20;   // adjust

reg [$clog2(CORDIC_LATENCY+1)-1:0] pipeline_cnt;
wire pipeline_filled = (pipeline_cnt == CORDIC_LATENCY);

wire signed [15:0] cordic_dx1, cordic_dy1;
wire signed [15:0] cordic_dx2, cordic_dy2;
wire [15:0] cordic_magn1, cordic_magn2;
wire [15:0] cordic_phase1, cordic_phase2;
wire cordic_valid1, cordic_valid2;

// CORDIC for source 1
cordic_0 cordic1 (
    .aclk(out_stream_aclk),
    .aresetn(periph_resetn),
    .s_axis_cartesian_tvalid(1'b1),
    .s_axis_cartesian_tdata({cordic_dy1, cordic_dx1}), // {Y, X} in IP's expected order
    .m_axis_dout_tvalid(cordic_valid1),
    .m_axis_dout_tdata({cordic_phase1, cordic_magn1})  // {phase, magnitude}
);

// CORDIC for source 2
cordic_0 cordic2 (
    .aclk(out_stream_aclk),
    .aresetn(periph_resetn),
    .s_axis_cartesian_tvalid(1'b1),
    .s_axis_cartesian_tdata({cordic_dy2, cordic_dx2}),
    .m_axis_dout_tvalid(cordic_valid2),
    .m_axis_dout_tdata({cordic_phase2, cordic_magn2})
);

// inputs to CORDICS
wire [9:0] dx1_abs = (x > SRC1_X) ? (x - SRC1_X) : (SRC1_X - x);
wire [8:0] dy1_abs = (y > SRC1_Y) ? (y - SRC1_Y) : (SRC1_Y - y);
wire [9:0] dx2_abs = (x > SRC2_X) ? (x - SRC2_X) : (SRC2_X - x);
wire [8:0] dy2_abs = (y > SRC2_Y) ? (y - SRC2_Y) : (SRC2_Y - y);

assign cordic_dx1 = {6'd0, dx1_abs};  // extend to 16 bits
assign cordic_dy1 = {7'd0, dy1_abs};
assign cordic_dx2 = {6'd0, dx2_abs};
assign cordic_dy2 = {7'd0, dy2_abs};

// delay line
reg [9:0]  x_dly [0:CORDIC_LATENCY-1];
reg [8:0]  y_dly [0:CORDIC_LATENCY-1];
reg        first_dly [0:CORDIC_LATENCY-1];
reg        lastx_dly [0:CORDIC_LATENCY-1];
reg        lasty_dly [0:CORDIC_LATENCY-1];
integer    i;

always @(posedge out_stream_aclk) begin
    if (!periph_resetn) begin
        for (i = 0; i < CORDIC_LATENCY; i = i+1) begin
            x_dly[i] <= 0;
            y_dly[i] <= 0;
            first_dly[i] <= 0;
            lastx_dly[i] <= 0;
            lasty_dly[i] <= 0;
        end
    end else begin
        // shift pipeline
        for (i = 1; i < CORDIC_LATENCY; i = i+1) begin
            x_dly[i] <= x_dly[i-1];
            y_dly[i] <= y_dly[i-1];
            first_dly[i] <= first_dly[i-1];
            lastx_dly[i] <= lastx_dly[i-1];
            lasty_dly[i] <= lasty_dly[i-1];
        end
        // input to pipeline
        x_dly[0] <= x;
        y_dly[0] <= y;
        first_dly[0] <= first;
        lastx_dly[0] <= lastx;
        lasty_dly[0] <= lasty;
    end
end

// Aligned signals after latency
wire [9:0]  x_aligned   = x_dly[CORDIC_LATENCY-1];
wire [8:0]  y_aligned   = y_dly[CORDIC_LATENCY-1];
wire        first_aligned = first_dly[CORDIC_LATENCY-1];
wire        lastx_aligned = lastx_dly[CORDIC_LATENCY-1];
wire        lasty_aligned = lasty_dly[CORDIC_LATENCY-1];


// wave computation using CORDIC magnitude
wire [15:0] dist1 = cordic_magn1;   // direct from CORDIC
wire [15:0] dist2 = cordic_magn2;

// reg file access
wire [7:0] current_time   = regfile[0][7:0];
wire [15:0] wave_front_dist = current_time * SPEED;

wire wave1_arrived = (dist1 <= wave_front_dist);
wire wave2_arrived = (dist2 <= wave_front_dist);
wire [15:0] phase1 = wave1_arrived ? (wave_front_dist - dist1) : 16'd0;
wire [15:0] phase2 = wave2_arrived ? (wave_front_dist - dist2) : 16'd0;


function signed [3:0] sine_lut;
    input [3:0] phase;
    case (phase)
        4'd0:  sine_lut =  4'sd0;
        4'd1:  sine_lut =  4'sd3;
        4'd2:  sine_lut =  4'sd5;
        4'd3:  sine_lut =  4'sd7;
        4'd4:  sine_lut =  4'sd7;
        4'd5:  sine_lut =  4'sd5;
        4'd6:  sine_lut =  4'sd3;
        4'd7:  sine_lut =  4'sd0;
        4'd8:  sine_lut = -4'sd3;
        4'd9:  sine_lut = -4'sd6;
        4'd10: sine_lut = -4'sd8;
        4'd11: sine_lut = -4'sd8;
        4'd12: sine_lut = -4'sd6;
        4'd13: sine_lut = -4'sd3;
        4'd14: sine_lut = -4'sd1;
        4'd15: sine_lut =  4'sd0;
        default: sine_lut = 4'sd0;
    endcase
endfunction

// Grid overlay
wire is_grid_line = ((x_aligned[5:0] == 6'b000000) || (y_aligned[5:0] == 6'b000000));

/* OLD CODE FROM NON CORDIC Ver.

// Time from register
wire [31:0]  current_time   = regfile[0];
wire [31:0] wave_front_dist = current_time * SPEED;

wire [9:0] dx1   = (x > SRC1_X) ? (x - SRC1_X) : (SRC1_X - x);
wire [8:0] dy1   = (y > SRC1_Y) ? (y - SRC1_Y) : (SRC1_Y - y);
wire [9:0] max_d1 = (dx1 > dy1) ? dx1 : dy1;
wire [9:0] min_d1 = (dx1 > dy1) ? dy1 : dx1;
wire [9:0] dist1  = max_d1 + ((min_d1 * 10'd3) >> 3);

wire wave1_arrived = (dist1 <= wave_front_dist);
wire [31:0] phase1 = wave1_arrived ? (wave_front_dist - dist1) : 32'd0;
*/

wire signed [3:0] raw_amp1  = sine_lut(phase1[5:2]);
wire [2:0] atten_shift1 = dist1[9:7];
wire [3:0] raw_abs1  = (raw_amp1 < 0) ? -raw_amp1 : raw_amp1;
wire [3:0] atten_abs1 = raw_abs1 >> atten_shift1;
wire signed [3:0] final_amp1 = (raw_amp1 < 0) ? -atten_abs1 : atten_abs1;

wire signed [3:0] raw_amp2  = sine_lut(phase2[5:2]);
wire [2:0] atten_shift2 = dist2[9:7];
wire [3:0] raw_abs2  = (raw_amp2 < 0) ? -raw_amp2 : raw_amp2;
wire [3:0] atten_abs2 = raw_abs2 >> atten_shift2;
wire signed [3:0] final_amp2 = (raw_amp2 < 0) ? -atten_abs2 : atten_abs2;

wire signed [5:0] super_amp  = final_amp1 + final_amp2;
wire signed [3:0] clamped_amp = super_amp >>> 2;

wire is_pos = (clamped_amp > 0);
wire is_neg = (clamped_amp < 0);
wire [3:0] abs_amp = is_pos ? clamped_amp : -clamped_amp;

wire [7:0] r_wave = is_pos ? (abs_amp * 8'd36) : (is_neg ? (abs_amp * 8'd16) : 8'd0);
wire [7:0] g_wave = is_pos ? (abs_amp * 8'd23) : 8'd0;
wire [7:0] b_wave = is_pos ? 8'd0              : (is_neg ? (abs_amp * 8'd16) : 8'd0);

wire show_wave = (wave1_arrived || wave2_arrived) && (abs_amp != 0);

wire [7:0] r, g, b;
assign r = show_wave ? r_wave : (is_grid_line ? 8'hFF : 8'h00);
assign g = show_wave ? g_wave : 8'h00;
assign b = show_wave ? b_wave : 8'h00;

packer pixel_packer(
    .aclk(out_stream_aclk),
    .aresetn(periph_resetn),
    .r(r), .g(g), .b(b),
    .eol(lastx_aligned),
    .in_stream_ready(ready),
    .valid(valid_int),
    .sof(first_aligned),
    .out_stream_tdata(out_stream_tdata),
    .out_stream_tkeep(out_stream_tkeep),
    .out_stream_tlast(out_stream_tlast),
    .out_stream_tready(out_stream_tready),
    .out_stream_tvalid(out_stream_tvalid),
    .out_stream_tuser(out_stream_tuser)
);

endmodule
