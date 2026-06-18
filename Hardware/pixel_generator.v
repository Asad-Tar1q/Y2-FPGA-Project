module pixel_generator #(
    parameter REG_FILE_SIZE       = 64,
    parameter AXI_LITE_ADDR_WIDTH = 8
)(
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

    input  [AXI_LITE_ADDR_WIDTH-1:0] s_axi_lite_araddr,
    output                           s_axi_lite_arready,
    input                            s_axi_lite_arvalid,
    input  [AXI_LITE_ADDR_WIDTH-1:0] s_axi_lite_awaddr,
    output                           s_axi_lite_awready,
    input                            s_axi_lite_awvalid,
    input                            s_axi_lite_bready,
    output [1:0]                     s_axi_lite_bresp,
    output                           s_axi_lite_bvalid,
    output [31:0]                    s_axi_lite_rdata,
    input                            s_axi_lite_rready,
    output [1:0]                     s_axi_lite_rresp,
    output                           s_axi_lite_rvalid,
    input  [31:0]                    s_axi_lite_wdata,
    output                           s_axi_lite_wready,
    input                            s_axi_lite_wvalid
);

localparam integer X_SIZE = 640;
localparam integer Y_SIZE = 480;
localparam integer REG_FILE_AWIDTH = $clog2(REG_FILE_SIZE);

localparam integer MAX_SRC   = 4;
localparam integer MAX_WALLS = 2;
localparam integer SRC_BASE  = 4;
localparam integer SRC_STRIDE = 4;
localparam integer WALL_BASE = 36;
localparam integer WALL_STRIDE = 3;

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

localparam [31:0] SPEED = 32'd4;
localparam integer FIFO_DEPTH = 512;
localparam integer FIFO_AWIDTH = $clog2(FIFO_DEPTH);
localparam integer CORDIC_PIPE_DEPTH = 32;

reg [31:0] regfile [0:REG_FILE_SIZE-1];
reg [REG_FILE_AWIDTH-1:0] writeAddr;
reg [REG_FILE_AWIDTH-1:0] readAddr;
reg [31:0] readData;
reg [31:0] writeData;
reg [1:0]  readState;
reg [2:0]  writeState;

integer axi_i;

always @(posedge s_axi_lite_aclk) begin
    if (!axi_resetn) begin
        readState <= AWAIT_RADD;
        readAddr  <= {REG_FILE_AWIDTH{1'b0}};
        readData  <= 32'd0;
    end else begin
        readData <= regfile[readAddr];
        case (readState)
            AWAIT_RADD: begin
                if (s_axi_lite_arvalid) begin
                    readAddr  <= s_axi_lite_araddr[2 +: REG_FILE_AWIDTH];
                    readState <= AWAIT_FETCH;
                end
            end
            AWAIT_FETCH: readState <= AWAIT_READ;
            AWAIT_READ: begin
                if (s_axi_lite_rready)
                    readState <= AWAIT_RADD;
            end
            default: readState <= AWAIT_RADD;
        endcase
    end
end

assign s_axi_lite_arready = (readState == AWAIT_RADD);
assign s_axi_lite_rresp   = (readAddr < REG_FILE_SIZE) ? AXI_OK : AXI_ERR;
assign s_axi_lite_rvalid  = (readState == AWAIT_READ);
assign s_axi_lite_rdata   = readData;

always @(posedge s_axi_lite_aclk) begin
    if (!axi_resetn) begin
        writeState <= AWAIT_WADD_AND_DATA;
        writeAddr  <= {REG_FILE_AWIDTH{1'b0}};
        writeData  <= 32'd0;
        for (axi_i = 0; axi_i < REG_FILE_SIZE; axi_i = axi_i + 1)
            regfile[axi_i] <= 32'd0;
    end else begin
        case (writeState)
            AWAIT_WADD_AND_DATA: begin
                case ({s_axi_lite_awvalid, s_axi_lite_wvalid})
                    2'b10: begin
                        writeAddr  <= s_axi_lite_awaddr[2 +: REG_FILE_AWIDTH];
                        writeState <= AWAIT_WDATA;
                    end
                    2'b01: begin
                        writeData  <= s_axi_lite_wdata;
                        writeState <= AWAIT_WADD;
                    end
                    2'b11: begin
                        writeData  <= s_axi_lite_wdata;
                        writeAddr  <= s_axi_lite_awaddr[2 +: REG_FILE_AWIDTH];
                        writeState <= AWAIT_WRITE;
                    end
                    default: writeState <= AWAIT_WADD_AND_DATA;
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
                    writeAddr  <= s_axi_lite_awaddr[2 +: REG_FILE_AWIDTH];
                    writeState <= AWAIT_WRITE;
                end
            end
            AWAIT_WRITE: begin
                if (writeAddr < REG_FILE_SIZE)
                    regfile[writeAddr] <= writeData;
                writeState <= AWAIT_RESP;
            end
            AWAIT_RESP: begin
                if (s_axi_lite_bready)
                    writeState <= AWAIT_WADD_AND_DATA;
            end
            default: writeState <= AWAIT_WADD_AND_DATA;
        endcase
    end
end

assign s_axi_lite_awready = (writeState == AWAIT_WADD_AND_DATA) || (writeState == AWAIT_WADD);
assign s_axi_lite_wready  = (writeState == AWAIT_WADD_AND_DATA) || (writeState == AWAIT_WDATA);
assign s_axi_lite_bvalid  = (writeState == AWAIT_RESP);
assign s_axi_lite_bresp   = (writeAddr < REG_FILE_SIZE) ? AXI_OK : AXI_ERR;

wire [31:0] current_time = regfile[0];
wire        paused       = regfile[1][0];

function [15:0] abs18;
    input signed [17:0] v;
    begin
        abs18 = (v < 0) ? -v : v;
    end
endfunction

function signed [15:0] sext16;
    input [15:0] v;
    begin
        sext16 = v;
    end
endfunction

function [15:0] approx_dist;
    input [15:0] dx;
    input [15:0] dy;
    reg [15:0] mx;
    reg [15:0] mn;
    begin
        mx = (dx > dy) ? dx : dy;
        mn = (dx > dy) ? dy : dx;
        approx_dist = mx + ((mn * 16'd3) >> 3);
    end
endfunction

function signed [4:0] sine_lut;
    input [3:0] phase;
    begin
        case (phase)
            4'd0:  sine_lut =  5'sd0;
            4'd1:  sine_lut =  5'sd3;
            4'd2:  sine_lut =  5'sd5;
            4'd3:  sine_lut =  5'sd7;
            4'd4:  sine_lut =  5'sd8;
            4'd5:  sine_lut =  5'sd7;
            4'd6:  sine_lut =  5'sd5;
            4'd7:  sine_lut =  5'sd3;
            4'd8:  sine_lut =  5'sd0;
            4'd9:  sine_lut = -5'sd3;
            4'd10: sine_lut = -5'sd5;
            4'd11: sine_lut = -5'sd7;
            4'd12: sine_lut = -5'sd8;
            4'd13: sine_lut = -5'sd7;
            4'd14: sine_lut = -5'sd5;
            4'd15: sine_lut = -5'sd3;
            default: sine_lut = 5'sd0;
        endcase
    end
endfunction

function axis_wall_hit;
    input signed [17:0] sx;
    input signed [17:0] sy;
    input signed [17:0] px;
    input signed [17:0] py;
    input signed [17:0] x0;
    input signed [17:0] y0;
    input signed [17:0] x1;
    input signed [17:0] y1;

    reg signed [17:0] xmin;
    reg signed [17:0] xmax;
    reg signed [17:0] ymin;
    reg signed [17:0] ymax;
    begin
        xmin = (x0 < x1) ? x0 : x1;
        xmax = (x0 < x1) ? x1 : x0;
        ymin = (y0 < y1) ? y0 : y1;
        ymax = (y0 < y1) ? y1 : y0;

        axis_wall_hit = 1'b0;

        if (x0 == x1) begin
            axis_wall_hit = (((sx < x0) && (px >= x0)) ||
                             ((sx > x0) && (px <= x0))) &&
                            (py >= ymin) && (py <= ymax);
        end else if (y0 == y1) begin
            axis_wall_hit = (((sy < y0) && (py >= y0)) ||
                             ((sy > y0) && (py <= y0))) &&
                            (px >= xmin) && (px <= xmax);
        end
    end
endfunction

reg [9:0] x_in;
reg [8:0] y_in;

wire input_lastx = (x_in == X_SIZE - 1);
wire input_lasty = (y_in == Y_SIZE - 1);

reg [7:0] scalar_fifo [0:FIFO_DEPTH-1];
reg [FIFO_AWIDTH-1:0] fifo_wr_ptr;
reg [FIFO_AWIDTH-1:0] fifo_rd_ptr;
reg [FIFO_AWIDTH:0]   fifo_count;

wire fifo_empty = (fifo_count == 0);
wire fifo_full  = (fifo_count == FIFO_DEPTH);
wire fifo_almost_full = (fifo_count >= (FIFO_DEPTH - CORDIC_PIPE_DEPTH - 8));
wire cordic_input_valid = periph_resetn && !paused && !fifo_almost_full;

always @(posedge out_stream_aclk) begin
    if (!periph_resetn) begin
        x_in <= 10'd0;
        y_in <= 9'd0;
    end else if (cordic_input_valid) begin
        if (input_lastx) begin
            x_in <= 10'd0;
            y_in <= input_lasty ? 9'd0 : y_in + 1'b1;
        end else begin
            x_in <= x_in + 1'b1;
        end
    end
end

wire signed [17:0] pix_x_in_s = {8'd0, x_in};
wire signed [17:0] pix_y_in_s = {9'd0, y_in};

wire signed [15:0] src_x [0:MAX_SRC-1];
wire signed [15:0] src_y [0:MAX_SRC-1];
wire        src_en [0:MAX_SRC-1];
wire        src_moving [0:MAX_SRC-1];
wire        src_virtual [0:MAX_SRC-1];
wire        src_phase_inv [0:MAX_SRC-1];
wire [3:0]  src_wall_id [0:MAX_SRC-1];
wire [7:0]  src_amp [0:MAX_SRC-1];
wire [7:0]  src_freq [0:MAX_SRC-1];
wire [7:0]  src_phase [0:MAX_SRC-1];
wire signed [7:0] src_dirx [0:MAX_SRC-1];
wire signed [7:0] src_diry [0:MAX_SRC-1];
wire [7:0] src_directivity [0:MAX_SRC-1];
wire signed [15:0] src_vx [0:MAX_SRC-1];
wire signed [15:0] src_vy [0:MAX_SRC-1];

wire signed [15:0] wall_x0 [0:MAX_WALLS-1];
wire signed [15:0] wall_y0 [0:MAX_WALLS-1];
wire signed [15:0] wall_x1 [0:MAX_WALLS-1];
wire signed [15:0] wall_y1 [0:MAX_WALLS-1];
wire wall_en [0:MAX_WALLS-1];
wire wall_reflect [0:MAX_WALLS-1];
wire wall_phase_inv [0:MAX_WALLS-1];
wire [7:0] wall_gain [0:MAX_WALLS-1];

wire [15:0] cordic_magn [0:MAX_SRC-1];
wire [15:0] cordic_phase [0:MAX_SRC-1];
wire [MAX_SRC-1:0] cordic_valid_vec;

wire signed [17:0] src_eff_x [0:MAX_SRC-1];
wire signed [17:0] src_eff_y [0:MAX_SRC-1];
wire [15:0] cordic_dx_abs [0:MAX_SRC-1];
wire [15:0] cordic_dy_abs [0:MAX_SRC-1];

reg [9:0] x_out;
reg [8:0] y_out;
wire signed [17:0] pix_x_out_s = {8'd0, x_out};
wire signed [17:0] pix_y_out_s = {9'd0, y_out};

wire cordic_output_valid = &cordic_valid_vec;

always @(posedge out_stream_aclk) begin
    if (!periph_resetn) begin
        x_out <= 10'd0;
        y_out <= 9'd0;
    end else if (cordic_output_valid) begin
        if (x_out == X_SIZE - 1) begin
            x_out <= 10'd0;
            y_out <= (y_out == Y_SIZE - 1) ? 9'd0 : y_out + 1'b1;
        end else begin
            x_out <= x_out + 1'b1;
        end
    end
end

genvar src_decode_i;
genvar wall_decode_i;
generate
    for (src_decode_i = 0; src_decode_i < MAX_SRC; src_decode_i = src_decode_i + 1) begin : SRC_DECODE
        localparam integer SBASE = SRC_BASE + src_decode_i * SRC_STRIDE;
        assign src_x[src_decode_i] = regfile[SBASE + 0][15:0];
        assign src_y[src_decode_i] = regfile[SBASE + 0][31:16];
        assign src_en[src_decode_i]        = regfile[SBASE + 1][0];
        assign src_moving[src_decode_i]    = regfile[SBASE + 1][1];
        assign src_virtual[src_decode_i]   = regfile[SBASE + 1][2];
        assign src_phase_inv[src_decode_i] = regfile[SBASE + 1][3];
        assign src_wall_id[src_decode_i]   = regfile[SBASE + 1][7:4];
        assign src_amp[src_decode_i]       = regfile[SBASE + 1][15:8];
        assign src_freq[src_decode_i]      = regfile[SBASE + 1][23:16];
        assign src_phase[src_decode_i]     = regfile[SBASE + 1][31:24];
        assign src_dirx[src_decode_i]      = regfile[SBASE + 2][7:0];
        assign src_diry[src_decode_i]      = regfile[SBASE + 2][15:8];
        assign src_directivity[src_decode_i] = regfile[SBASE + 2][23:16];
        assign src_vx[src_decode_i]        = regfile[SBASE + 3][15:0];
        assign src_vy[src_decode_i]        = regfile[SBASE + 3][31:16];
    end

    for (wall_decode_i = 0; wall_decode_i < MAX_WALLS; wall_decode_i = wall_decode_i + 1) begin : WALL_DECODE
        localparam integer WBASE = WALL_BASE + wall_decode_i * WALL_STRIDE;
        assign wall_x0[wall_decode_i] = regfile[WBASE + 0][15:0];
        assign wall_y0[wall_decode_i] = regfile[WBASE + 0][31:16];
        assign wall_x1[wall_decode_i] = regfile[WBASE + 1][15:0];
        assign wall_y1[wall_decode_i] = regfile[WBASE + 1][31:16];
        assign wall_en[wall_decode_i]        = regfile[WBASE + 2][0];
        assign wall_reflect[wall_decode_i]   = regfile[WBASE + 2][1];
        assign wall_phase_inv[wall_decode_i] = regfile[WBASE + 2][2];
        assign wall_gain[wall_decode_i]      = regfile[WBASE + 2][15:8];
    end
endgenerate

genvar cordic_i;
generate
    for (cordic_i = 0; cordic_i < MAX_SRC; cordic_i = cordic_i + 1) begin : CORDIC_LANES
        wire signed [17:0] sx_ext = {{2{src_x[cordic_i][15]}}, src_x[cordic_i]};
        wire signed [17:0] sy_ext = {{2{src_y[cordic_i][15]}}, src_y[cordic_i]};
        wire signed [17:0] dx0_s = pix_x_in_s - sx_ext;
        wire signed [17:0] dy0_s = pix_y_in_s - sy_ext;
        wire [15:0] dx0_abs = abs18(dx0_s);
        wire [15:0] dy0_abs = abs18(dy0_s);
        wire [15:0] dist0 = approx_dist(dx0_abs, dy0_abs);
        wire [15:0] tau0 = dist0 >> 2;
        wire signed [31:0] move_x = src_vx[cordic_i] * $signed({1'b0, tau0});
        wire signed [31:0] move_y = src_vy[cordic_i] * $signed({1'b0, tau0});
        wire signed [17:0] ret_x = sx_ext - (move_x >>> 4);
        wire signed [17:0] ret_y = sy_ext - (move_y >>> 4);

        assign src_eff_x[cordic_i] = (src_moving[cordic_i] && !src_virtual[cordic_i]) ? ret_x : sx_ext;
        assign src_eff_y[cordic_i] = (src_moving[cordic_i] && !src_virtual[cordic_i]) ? ret_y : sy_ext;

        wire signed [17:0] dx_eff_s = pix_x_in_s - src_eff_x[cordic_i];
        wire signed [17:0] dy_eff_s = pix_y_in_s - src_eff_y[cordic_i];
        assign cordic_dx_abs[cordic_i] = abs18(dx_eff_s);
        assign cordic_dy_abs[cordic_i] = abs18(dy_eff_s);

        cordic_0 cordic_inst (
            .aclk                   (out_stream_aclk),
            .aresetn                (periph_resetn),
            .s_axis_cartesian_tvalid(cordic_input_valid),
            .s_axis_cartesian_tdata ({cordic_dy_abs[cordic_i], cordic_dx_abs[cordic_i]}),
            .m_axis_dout_tvalid     (cordic_valid_vec[cordic_i]),
            .m_axis_dout_tdata      ({cordic_phase[cordic_i], cordic_magn[cordic_i]})
        );
    end
endgenerate

wire signed [15:0] contrib [0:MAX_SRC-1];

genvar contrib_i;
genvar contrib_wall_i;
generate
    for (contrib_i = 0; contrib_i < MAX_SRC; contrib_i = contrib_i + 1) begin : CONTRIB
        wire [MAX_WALLS-1:0] absorber_hit;
        wire [MAX_WALLS-1:0] reflector_hit;
        wire signed [17:0] src_x_out_s = {{2{src_x[contrib_i][15]}}, src_x[contrib_i]};
        wire signed [17:0] src_y_out_s = {{2{src_y[contrib_i][15]}}, src_y[contrib_i]};

        for (contrib_wall_i = 0; contrib_wall_i < MAX_WALLS; contrib_wall_i = contrib_wall_i + 1) begin : WALL_TESTS
            wire signed [17:0] wx0_s = {{2{wall_x0[contrib_wall_i][15]}}, wall_x0[contrib_wall_i]};
            wire signed [17:0] wy0_s = {{2{wall_y0[contrib_wall_i][15]}}, wall_y0[contrib_wall_i]};
            wire signed [17:0] wx1_s = {{2{wall_x1[contrib_wall_i][15]}}, wall_x1[contrib_wall_i]};
            wire signed [17:0] wy1_s = {{2{wall_y1[contrib_wall_i][15]}}, wall_y1[contrib_wall_i]};
            wire hit = axis_wall_hit(src_x_out_s, src_y_out_s, pix_x_out_s, pix_y_out_s,
                                     wx0_s, wy0_s, wx1_s, wy1_s);
            assign absorber_hit[contrib_wall_i]  = wall_en[contrib_wall_i] && !wall_reflect[contrib_wall_i] && hit;
            assign reflector_hit[contrib_wall_i] = wall_en[contrib_wall_i] &&  wall_reflect[contrib_wall_i] && hit;
        end

        wire blocked = (|absorber_hit) || ((!src_virtual[contrib_i]) && (|reflector_hit));
        wire reflector_ok = (src_wall_id[contrib_i][0] == 1'b0) ? reflector_hit[0] : reflector_hit[1];
        wire path_ok = (!src_virtual[contrib_i]) || reflector_ok;

        wire [15:0] dist = cordic_magn[contrib_i];
        wire [31:0] wave_front_dist = current_time * SPEED;
        wire arrived = ({16'd0, dist} <= wave_front_dist);
        wire [31:0] phase_delta = arrived ? (wave_front_dist - {16'd0, dist}) : 32'd0;
        wire [7:0] freq_q = (src_freq[contrib_i] == 8'd0) ? 8'd16 : src_freq[contrib_i];
        wire [39:0] phase_mult = phase_delta * freq_q;
        wire [31:0] phase_arg = (phase_mult >> 4) + {24'd0, src_phase[contrib_i]} + (src_phase_inv[contrib_i] ? 32'd32 : 32'd0);
        wire signed [4:0] raw_amp = sine_lut(phase_arg[5:2]);
        wire signed [5:0] raw_amp_ext = {raw_amp[4], raw_amp};
        wire [5:0] raw_abs = raw_amp_ext[5] ? -raw_amp_ext : raw_amp_ext;
        wire [2:0] atten_shift = dist[10:8];
        wire [5:0] atten_abs = raw_abs >> atten_shift;
        wire signed [6:0] atten_mag = $signed({1'b0, atten_abs});
        wire signed [6:0] atten_signed = raw_amp_ext[5] ? -atten_mag : atten_mag;

        wire signed [17:0] dx_dir = pix_x_out_s - src_x_out_s;
        wire signed [17:0] dy_dir = pix_y_out_s - src_y_out_s;
        wire signed [31:0] dot_x = dx_dir * src_dirx[contrib_i];
        wire signed [31:0] dot_y = dy_dir * src_diry[contrib_i];
        wire front_lobe = ((dot_x + dot_y) >= 0);
        wire [7:0] dir_gain = front_lobe ? 8'd255 : (8'd255 - src_directivity[contrib_i]);
        wire signed [16:0] amp_mult = atten_signed * $signed({1'b0, src_amp[contrib_i]});
        wire signed [25:0] dir_mult = amp_mult * $signed({1'b0, dir_gain});
        wire signed [15:0] scaled = dir_mult >>> 14;

        assign contrib[contrib_i] = (src_en[contrib_i] && arrived && !blocked && path_ok) ? scaled : 16'sd0;
    end
endgenerate

wire signed [19:0] contrib_sum =
    contrib[0] + contrib[1] + contrib[2] + contrib[3];

wire signed [20:0] scalar_tmp = 21'sd128 + contrib_sum;
wire [7:0] scalar_wave_value = (scalar_tmp < 21'sd0) ? 8'd0 :
                               (scalar_tmp > 21'sd255) ? 8'd255 :
                               scalar_tmp[7:0];

// Optional hardware debug: set regfile[1][24] from software to display
// the source-0 / wall-0 hit mask directly.
wire debug_wall_mask = regfile[1][24];
wire signed [17:0] dbg_sx  = {{2{src_x[0][15]}}, src_x[0]};
wire signed [17:0] dbg_sy  = {{2{src_y[0][15]}}, src_y[0]};
wire signed [17:0] dbg_wx0 = {{2{wall_x0[0][15]}}, wall_x0[0]};
wire signed [17:0] dbg_wy0 = {{2{wall_y0[0][15]}}, wall_y0[0]};
wire signed [17:0] dbg_wx1 = {{2{wall_x1[0][15]}}, wall_x1[0]};
wire signed [17:0] dbg_wy1 = {{2{wall_y1[0][15]}}, wall_y1[0]};
wire debug_wall0_hit = wall_en[0] && axis_wall_hit(dbg_sx, dbg_sy,
                                                    pix_x_out_s, pix_y_out_s,
                                                    dbg_wx0, dbg_wy0,
                                                    dbg_wx1, dbg_wy1);
wire [7:0] scalar_value = debug_wall_mask ? (debug_wall0_hit ? 8'd255 : 8'd0) : scalar_wave_value;

wire fifo_wr_en = cordic_output_valid && !fifo_full;
wire packer_ready;
wire fifo_rd_en = !fifo_empty && packer_ready;
wire [7:0] fifo_scalar = scalar_fifo[fifo_rd_ptr];

always @(posedge out_stream_aclk) begin
    if (!periph_resetn) begin
        fifo_wr_ptr <= {FIFO_AWIDTH{1'b0}};
        fifo_rd_ptr <= {FIFO_AWIDTH{1'b0}};
        fifo_count  <= {(FIFO_AWIDTH+1){1'b0}};
    end else begin
        if (fifo_wr_en) begin
            scalar_fifo[fifo_wr_ptr] <= scalar_value;
            fifo_wr_ptr <= fifo_wr_ptr + 1'b1;
        end

        if (fifo_rd_en)
            fifo_rd_ptr <= fifo_rd_ptr + 1'b1;

        case ({fifo_wr_en, fifo_rd_en})
            2'b10: fifo_count <= fifo_count + 1'b1;
            2'b01: fifo_count <= fifo_count - 1'b1;
            default: fifo_count <= fifo_count;
        endcase
    end
end

scalar_packer #(
    .X_SIZE(X_SIZE),
    .Y_SIZE(Y_SIZE)
) scalar_pack_inst (
    .clk         (out_stream_aclk),
    .rst_n       (periph_resetn),
    .scalar_in   (fifo_scalar),
    .pixel_valid (!fifo_empty),
    .pixel_ready (packer_ready),
    .tdata       (out_stream_tdata),
    .tkeep       (out_stream_tkeep),
    .tvalid      (out_stream_tvalid),
    .tready      (out_stream_tready),
    .tlast       (out_stream_tlast),
    .tuser       (out_stream_tuser[0])
);

endmodule
