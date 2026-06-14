module pixel_generator #(
    parameter REG_FILE_SIZE        = 32,
    parameter AXI_LITE_ADDR_WIDTH  = 8
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

localparam X_SIZE = 640;
localparam Y_SIZE = 480;

localparam REG_FILE_AWIDTH = $clog2(REG_FILE_SIZE);

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
localparam PROP_SPEED_SHIFT = 2;

localparam signed [15:0] MOVING_VX = 16'sd1;
localparam signed [15:0] MOVING_VY = 16'sd0;
localparam MOVE_SHIFT = 3;

localparam CORDIC_PIPE_DEPTH = 32;
localparam FIFO_DEPTH  = 256;
localparam FIFO_AWIDTH = $clog2(FIFO_DEPTH);
localparam FIFO_WIDTH  = 26;

function [15:0] abs_diff16;
    input signed [15:0] a;
    input signed [15:0] b;
    reg signed [16:0] d;
    begin
        d = {a[15], a} - {b[15], b};
        abs_diff16 = (d < 0) ? -d : d;
    end
endfunction

function [15:0] approx_dist;
    input signed [15:0] ax;
    input signed [15:0] ay;
    input signed [15:0] bx;
    input signed [15:0] by;
    reg [15:0] dx;
    reg [15:0] dy;
    reg [15:0] maxd;
    reg [15:0] mind;
    begin
        dx = abs_diff16(ax, bx);
        dy = abs_diff16(ay, by);
        maxd = (dx > dy) ? dx : dy;
        mind = (dx > dy) ? dy : dx;
        approx_dist = maxd + ((mind * 16'd3) >> 3);
    end
endfunction

function signed [15:0] clip_x;
    input signed [31:0] v;
    begin
        if (v < 0)
            clip_x = 16'sd0;
        else if (v > (X_SIZE - 1))
            clip_x = X_SIZE - 1;
        else
            clip_x = v[15:0];
    end
endfunction

function signed [15:0] clip_y;
    input signed [31:0] v;
    begin
        if (v < 0)
            clip_y = 16'sd0;
        else if (v > (Y_SIZE - 1))
            clip_y = Y_SIZE - 1;
        else
            clip_y = v[15:0];
    end
endfunction

function signed [15:0] moving_x_at_time;
    input signed [15:0] base;
    input [31:0] t;
    reg signed [31:0] step;
    reg signed [31:0] val;
    begin
        step = t >> MOVE_SHIFT;
        val = {{16{base[15]}}, base} + (step * MOVING_VX);
        moving_x_at_time = clip_x(val);
    end
endfunction

function signed [15:0] moving_y_at_time;
    input signed [15:0] base;
    input [31:0] t;
    reg signed [31:0] step;
    reg signed [31:0] val;
    begin
        step = t >> MOVE_SHIFT;
        val = {{16{base[15]}}, base} + (step * MOVING_VY);
        moving_y_at_time = clip_y(val);
    end
endfunction

function [31:0] retarded_time;
    input [31:0] t;
    input [15:0] dist;
    reg [31:0] delay;
    begin
        delay = dist >> PROP_SPEED_SHIFT;
        retarded_time = (t > delay) ? (t - delay) : 32'd0;
    end
endfunction

function signed [15:0] smin16;
    input signed [15:0] a;
    input signed [15:0] b;
    begin
        smin16 = (a < b) ? a : b;
    end
endfunction

function signed [15:0] smax16;
    input signed [15:0] a;
    input signed [15:0] b;
    begin
        smax16 = (a > b) ? a : b;
    end
endfunction

function signed [31:0] orient2d;
    input signed [15:0] ax;
    input signed [15:0] ay;
    input signed [15:0] bx;
    input signed [15:0] by;
    input signed [15:0] cx;
    input signed [15:0] cy;
    reg signed [31:0] bax;
    reg signed [31:0] bay;
    reg signed [31:0] cax;
    reg signed [31:0] cay;
    begin
        bax = bx - ax;
        bay = by - ay;
        cax = cx - ax;
        cay = cy - ay;
        orient2d = (bax * cay) - (bay * cax);
    end
endfunction

function segment_intersects;
    input signed [15:0] ax;
    input signed [15:0] ay;
    input signed [15:0] bx;
    input signed [15:0] by;
    input signed [15:0] cx;
    input signed [15:0] cy;
    input signed [15:0] dx;
    input signed [15:0] dy;
    reg signed [31:0] o1;
    reg signed [31:0] o2;
    reg signed [31:0] o3;
    reg signed [31:0] o4;
    reg bbox;
    begin
        o1 = orient2d(ax, ay, bx, by, cx, cy);
        o2 = orient2d(ax, ay, bx, by, dx, dy);
        o3 = orient2d(cx, cy, dx, dy, ax, ay);
        o4 = orient2d(cx, cy, dx, dy, bx, by);

        bbox = (smax16(smin16(ax, bx), smin16(cx, dx)) <= smin16(smax16(ax, bx), smax16(cx, dx))) &&
               (smax16(smin16(ay, by), smin16(cy, dy)) <= smin16(smax16(ay, by), smax16(cy, dy)));

        segment_intersects = bbox &&
            (((o1 <= 0) && (o2 >= 0)) || ((o1 >= 0) && (o2 <= 0))) &&
            (((o3 <= 0) && (o4 >= 0)) || ((o3 >= 0) && (o4 <= 0)));
    end
endfunction

function signed [15:0] mirror_x;
    input signed [15:0] sx;
    input signed [15:0] line_x;
    input horizontal;
    reg signed [31:0] v;
    begin
        if (horizontal) begin
            mirror_x = sx;
        end else begin
            v = ({{16{line_x[15]}}, line_x} <<< 1) - {{16{sx[15]}}, sx};
            mirror_x = v[15:0];
        end
    end
endfunction

function signed [15:0] mirror_y;
    input signed [15:0] sy;
    input signed [15:0] line_y;
    input horizontal;
    reg signed [31:0] v;
    begin
        if (horizontal) begin
            v = ({{16{line_y[15]}}, line_y} <<< 1) - {{16{sy[15]}}, sy};
            mirror_y = v[15:0];
        end else begin
            mirror_y = sy;
        end
    end
endfunction

function signed [3:0] sine_lut;
    input [3:0] phase;
    begin
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
    end
endfunction

function signed [7:0] wave_contrib;
    input [15:0] dist;
    input [31:0] time_value;
    input [3:0] src_gain;
    input [3:0] phase_offset;
    input enabled;
    input [3:0] path_gain;
    input invert;
    reg [31:0] wave_front;
    reg [31:0] phase;
    reg signed [3:0] raw;
    reg [3:0] raw_abs;
    reg [3:0] atten_abs;
    reg signed [4:0] atten_signed;
    reg signed [15:0] scaled;
    begin
        wave_front = time_value * SPEED;

        if (!enabled || ({16'd0, dist} > wave_front)) begin
            wave_contrib = 8'sd0;
        end else begin
            phase = wave_front - {16'd0, dist};
            raw = sine_lut(phase[5:2] + phase_offset);
            raw_abs = (raw < 0) ? -raw : raw;
            atten_abs = raw_abs >> dist[9:7];
            atten_signed = (raw < 0) ? -$signed({1'b0, atten_abs}) : $signed({1'b0, atten_abs});
            scaled = atten_signed * $signed({1'b0, src_gain}) * $signed({1'b0, path_gain});
            wave_contrib = invert ? -$signed(scaled >>> 6) : $signed(scaled >>> 6);
        end
    end
endfunction

function signed [5:0] clamp6;
    input signed [11:0] v;
    begin
        if (v > 12'sd31)
            clamp6 = 6'sd31;
        else if (v < -12'sd32)
            clamp6 = -6'sd32;
        else
            clamp6 = v[5:0];
    end
endfunction

reg [31:0]                regfile [REG_FILE_SIZE-1:0];
reg [REG_FILE_AWIDTH-1:0] writeAddr;
reg [REG_FILE_AWIDTH-1:0] readAddr;
reg [31:0]                readData;
reg [31:0]                writeData;
reg [1:0]                 readState;
reg [2:0]                 writeState;

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
end

assign s_axi_lite_arready = (readState == AWAIT_RADD);
assign s_axi_lite_rresp   = AXI_OK;
assign s_axi_lite_rvalid  = (readState == AWAIT_READ);
assign s_axi_lite_rdata   = readData;

always @(posedge s_axi_lite_aclk) begin
    if (!axi_resetn) begin
        writeState <= AWAIT_WADD_AND_DATA;
        writeAddr  <= {REG_FILE_AWIDTH{1'b0}};
        writeData  <= 32'd0;

        for (axi_i = 0; axi_i < REG_FILE_SIZE; axi_i = axi_i + 1) begin
            regfile[axi_i] <= 32'd0;
        end

        regfile[1]  <= (32'd240 << 16) | 32'd200;
        regfile[2]  <= (32'd240 << 16) | 32'd440;
        regfile[3]  <= 32'h00000801;
        regfile[4]  <= 32'h00000801;
        regfile[13] <= 32'h00000400;
        regfile[16] <= 32'h00000400;
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
                    writeAddr  <= s_axi_lite_awaddr[2 +: REG_FILE_AWIDTH];
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
end

assign s_axi_lite_awready = (writeState == AWAIT_WADD_AND_DATA) ||
                            (writeState == AWAIT_WADD);

assign s_axi_lite_wready  = (writeState == AWAIT_WADD_AND_DATA) ||
                            (writeState == AWAIT_WDATA);

assign s_axi_lite_bvalid  = (writeState == AWAIT_RESP);
assign s_axi_lite_bresp   = AXI_OK;

wire [31:0] current_time = regfile[0];

wire signed [15:0] src1_x0 = regfile[1][15:0];
wire signed [15:0] src1_y0 = regfile[1][31:16];
wire signed [15:0] src2_x0 = regfile[2][15:0];
wire signed [15:0] src2_y0 = regfile[2][31:16];

wire src1_enable = regfile[3][0];
wire src1_moving = regfile[3][1];
wire [3:0] src1_phase = regfile[3][7:4];
wire [3:0] src1_gain  = regfile[3][11:8];

wire src2_enable = regfile[4][0];
wire src2_moving = regfile[4][1];
wire [3:0] src2_phase = regfile[4][7:4];
wire [3:0] src2_gain  = regfile[4][11:8];

wire signed [15:0] blk0_x0 = regfile[5][15:0];
wire signed [15:0] blk0_y0 = regfile[5][31:16];
wire signed [15:0] blk0_x1 = regfile[6][15:0];
wire signed [15:0] blk0_y1 = regfile[6][31:16];
wire blk0_enable = regfile[7][0];

wire signed [15:0] blk1_x0 = regfile[8][15:0];
wire signed [15:0] blk1_y0 = regfile[8][31:16];
wire signed [15:0] blk1_x1 = regfile[9][15:0];
wire signed [15:0] blk1_y1 = regfile[9][31:16];
wire blk1_enable = regfile[10][0];

wire signed [15:0] refl0_x0 = regfile[11][15:0];
wire signed [15:0] refl0_y0 = regfile[11][31:16];
wire signed [15:0] refl0_x1 = regfile[12][15:0];
wire signed [15:0] refl0_y1 = regfile[12][31:16];
wire refl0_enable = regfile[13][0];
wire [3:0] refl0_gain = regfile[13][11:8];
wire refl0_invert = regfile[13][12];
wire refl0_horizontal = (refl0_y0 == refl0_y1);

wire signed [15:0] refl1_x0 = regfile[14][15:0];
wire signed [15:0] refl1_y0 = regfile[14][31:16];
wire signed [15:0] refl1_x1 = regfile[15][15:0];
wire signed [15:0] refl1_y1 = regfile[15][31:16];
wire refl1_enable = regfile[16][0];
wire [3:0] refl1_gain = regfile[16][11:8];
wire refl1_invert = regfile[16][12];
wire refl1_horizontal = (refl1_y0 == refl1_y1);

reg [9:0] x_in;
reg [8:0] y_in;

wire input_lastx = (x_in == X_SIZE - 1);
wire input_lasty = (y_in == Y_SIZE - 1);

reg [FIFO_WIDTH-1:0] pixel_fifo [0:FIFO_DEPTH-1];
reg [FIFO_AWIDTH-1:0] fifo_wr_ptr;
reg [FIFO_AWIDTH-1:0] fifo_rd_ptr;
reg [FIFO_AWIDTH:0]   fifo_count;

wire fifo_empty = (fifo_count == 0);
wire fifo_full  = (fifo_count == FIFO_DEPTH);
wire fifo_valid = !fifo_empty;
wire fifo_almost_full = (fifo_count >= (FIFO_DEPTH - CORDIC_PIPE_DEPTH - 2));
wire cordic_input_valid = periph_resetn && !fifo_almost_full;

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

wire signed [15:0] pix_x_in = {6'd0, x_in};
wire signed [15:0] pix_y_in = {7'd0, y_in};

wire signed [15:0] src1_x_now_in = src1_moving ? moving_x_at_time(src1_x0, current_time) : src1_x0;
wire signed [15:0] src1_y_now_in = src1_moving ? moving_y_at_time(src1_y0, current_time) : src1_y0;
wire signed [15:0] src2_x_now_in = src2_moving ? moving_x_at_time(src2_x0, current_time) : src2_x0;
wire signed [15:0] src2_y_now_in = src2_moving ? moving_y_at_time(src2_y0, current_time) : src2_y0;

wire [15:0] src1_d0_in = approx_dist(pix_x_in, pix_y_in, src1_x_now_in, src1_y_now_in);
wire [15:0] src2_d0_in = approx_dist(pix_x_in, pix_y_in, src2_x_now_in, src2_y_now_in);

wire [31:0] src1_t_ret_in = retarded_time(current_time, src1_d0_in);
wire [31:0] src2_t_ret_in = retarded_time(current_time, src2_d0_in);

wire signed [15:0] src1_x_eff_in = src1_moving ? moving_x_at_time(src1_x0, src1_t_ret_in) : src1_x0;
wire signed [15:0] src1_y_eff_in = src1_moving ? moving_y_at_time(src1_y0, src1_t_ret_in) : src1_y0;
wire signed [15:0] src2_x_eff_in = src2_moving ? moving_x_at_time(src2_x0, src2_t_ret_in) : src2_x0;
wire signed [15:0] src2_y_eff_in = src2_moving ? moving_y_at_time(src2_y0, src2_t_ret_in) : src2_y0;

wire signed [15:0] src1_refl0_x = mirror_x(src1_x0, refl0_x0, refl0_horizontal);
wire signed [15:0] src1_refl0_y = mirror_y(src1_y0, refl0_y0, refl0_horizontal);
wire signed [15:0] src2_refl0_x = mirror_x(src2_x0, refl0_x0, refl0_horizontal);
wire signed [15:0] src2_refl0_y = mirror_y(src2_y0, refl0_y0, refl0_horizontal);

wire signed [15:0] src1_refl1_x = mirror_x(src1_x0, refl1_x0, refl1_horizontal);
wire signed [15:0] src1_refl1_y = mirror_y(src1_y0, refl1_y0, refl1_horizontal);
wire signed [15:0] src2_refl1_x = mirror_x(src2_x0, refl1_x0, refl1_horizontal);
wire signed [15:0] src2_refl1_y = mirror_y(src2_y0, refl1_y0, refl1_horizontal);

wire signed [15:0] dx_dir1  = abs_diff16(pix_x_in, src1_x_eff_in);
wire signed [15:0] dy_dir1  = abs_diff16(pix_y_in, src1_y_eff_in);
wire signed [15:0] dx_dir2  = abs_diff16(pix_x_in, src2_x_eff_in);
wire signed [15:0] dy_dir2  = abs_diff16(pix_y_in, src2_y_eff_in);

wire signed [15:0] dx_r10   = abs_diff16(pix_x_in, src1_refl0_x);
wire signed [15:0] dy_r10   = abs_diff16(pix_y_in, src1_refl0_y);
wire signed [15:0] dx_r20   = abs_diff16(pix_x_in, src2_refl0_x);
wire signed [15:0] dy_r20   = abs_diff16(pix_y_in, src2_refl0_y);
wire signed [15:0] dx_r11   = abs_diff16(pix_x_in, src1_refl1_x);
wire signed [15:0] dy_r11   = abs_diff16(pix_y_in, src1_refl1_y);
wire signed [15:0] dx_r21   = abs_diff16(pix_x_in, src2_refl1_x);
wire signed [15:0] dy_r21   = abs_diff16(pix_y_in, src2_refl1_y);

wire [15:0] dist_dir1;
wire [15:0] dist_dir2;
wire [15:0] dist_r10;
wire [15:0] dist_r20;
wire [15:0] dist_r11;
wire [15:0] dist_r21;

wire [15:0] phase_unused_dir1;
wire [15:0] phase_unused_dir2;
wire [15:0] phase_unused_r10;
wire [15:0] phase_unused_r20;
wire [15:0] phase_unused_r11;
wire [15:0] phase_unused_r21;

wire valid_dir1;
wire valid_dir2;
wire valid_r10;
wire valid_r20;
wire valid_r11;
wire valid_r21;

cordic_0 cordic_dir1 (
    .aclk                  (out_stream_aclk),
    .aresetn               (periph_resetn),
    .s_axis_cartesian_tvalid(cordic_input_valid),
    .s_axis_cartesian_tdata ({dy_dir1, dx_dir1}),
    .m_axis_dout_tvalid    (valid_dir1),
    .m_axis_dout_tdata     ({phase_unused_dir1, dist_dir1})
);

cordic_0 cordic_dir2 (
    .aclk                  (out_stream_aclk),
    .aresetn               (periph_resetn),
    .s_axis_cartesian_tvalid(cordic_input_valid),
    .s_axis_cartesian_tdata ({dy_dir2, dx_dir2}),
    .m_axis_dout_tvalid    (valid_dir2),
    .m_axis_dout_tdata     ({phase_unused_dir2, dist_dir2})
);

cordic_0 cordic_r10 (
    .aclk                  (out_stream_aclk),
    .aresetn               (periph_resetn),
    .s_axis_cartesian_tvalid(cordic_input_valid),
    .s_axis_cartesian_tdata ({dy_r10, dx_r10}),
    .m_axis_dout_tvalid    (valid_r10),
    .m_axis_dout_tdata     ({phase_unused_r10, dist_r10})
);

cordic_0 cordic_r20 (
    .aclk                  (out_stream_aclk),
    .aresetn               (periph_resetn),
    .s_axis_cartesian_tvalid(cordic_input_valid),
    .s_axis_cartesian_tdata ({dy_r20, dx_r20}),
    .m_axis_dout_tvalid    (valid_r20),
    .m_axis_dout_tdata     ({phase_unused_r20, dist_r20})
);

cordic_0 cordic_r11 (
    .aclk                  (out_stream_aclk),
    .aresetn               (periph_resetn),
    .s_axis_cartesian_tvalid(cordic_input_valid),
    .s_axis_cartesian_tdata ({dy_r11, dx_r11}),
    .m_axis_dout_tvalid    (valid_r11),
    .m_axis_dout_tdata     ({phase_unused_r11, dist_r11})
);

cordic_0 cordic_r21 (
    .aclk                  (out_stream_aclk),
    .aresetn               (periph_resetn),
    .s_axis_cartesian_tvalid(cordic_input_valid),
    .s_axis_cartesian_tdata ({dy_r21, dx_r21}),
    .m_axis_dout_tvalid    (valid_r21),
    .m_axis_dout_tdata     ({phase_unused_r21, dist_r21})
);

wire cordic_output_valid = valid_dir1 && valid_dir2 && valid_r10 && valid_r20 && valid_r11 && valid_r21;

reg [9:0] x_out;
reg [8:0] y_out;

wire output_first = (x_out == 0) && (y_out == 0);
wire output_lastx = (x_out == X_SIZE - 1);
wire output_lasty = (y_out == Y_SIZE - 1);

always @(posedge out_stream_aclk) begin
    if (!periph_resetn) begin
        x_out <= 10'd0;
        y_out <= 9'd0;
    end else if (cordic_output_valid) begin
        if (output_lastx) begin
            x_out <= 10'd0;
            y_out <= output_lasty ? 9'd0 : y_out + 1'b1;
        end else begin
            x_out <= x_out + 1'b1;
        end
    end
end

wire signed [15:0] pix_x_out = {6'd0, x_out};
wire signed [15:0] pix_y_out = {7'd0, y_out};

wire signed [15:0] src1_x_now_out = src1_moving ? moving_x_at_time(src1_x0, current_time) : src1_x0;
wire signed [15:0] src1_y_now_out = src1_moving ? moving_y_at_time(src1_y0, current_time) : src1_y0;
wire signed [15:0] src2_x_now_out = src2_moving ? moving_x_at_time(src2_x0, current_time) : src2_x0;
wire signed [15:0] src2_y_now_out = src2_moving ? moving_y_at_time(src2_y0, current_time) : src2_y0;

wire [15:0] src1_d0_out = approx_dist(pix_x_out, pix_y_out, src1_x_now_out, src1_y_now_out);
wire [15:0] src2_d0_out = approx_dist(pix_x_out, pix_y_out, src2_x_now_out, src2_y_now_out);

wire [31:0] src1_t_ret_out = retarded_time(current_time, src1_d0_out);
wire [31:0] src2_t_ret_out = retarded_time(current_time, src2_d0_out);

wire signed [15:0] src1_x_eff_out = src1_moving ? moving_x_at_time(src1_x0, src1_t_ret_out) : src1_x0;
wire signed [15:0] src1_y_eff_out = src1_moving ? moving_y_at_time(src1_y0, src1_t_ret_out) : src1_y0;
wire signed [15:0] src2_x_eff_out = src2_moving ? moving_x_at_time(src2_x0, src2_t_ret_out) : src2_x0;
wire signed [15:0] src2_y_eff_out = src2_moving ? moving_y_at_time(src2_y0, src2_t_ret_out) : src2_y0;

wire src1_blocked = (blk0_enable && segment_intersects(src1_x_eff_out, src1_y_eff_out, pix_x_out, pix_y_out, blk0_x0, blk0_y0, blk0_x1, blk0_y1)) ||
                    (blk1_enable && segment_intersects(src1_x_eff_out, src1_y_eff_out, pix_x_out, pix_y_out, blk1_x0, blk1_y0, blk1_x1, blk1_y1));

wire src2_blocked = (blk0_enable && segment_intersects(src2_x_eff_out, src2_y_eff_out, pix_x_out, pix_y_out, blk0_x0, blk0_y0, blk0_x1, blk0_y1)) ||
                    (blk1_enable && segment_intersects(src2_x_eff_out, src2_y_eff_out, pix_x_out, pix_y_out, blk1_x0, blk1_y0, blk1_x1, blk1_y1));

wire src1_refl0_valid = refl0_enable && !src1_moving && segment_intersects(src1_refl0_x, src1_refl0_y, pix_x_out, pix_y_out, refl0_x0, refl0_y0, refl0_x1, refl0_y1);
wire src2_refl0_valid = refl0_enable && !src2_moving && segment_intersects(src2_refl0_x, src2_refl0_y, pix_x_out, pix_y_out, refl0_x0, refl0_y0, refl0_x1, refl0_y1);
wire src1_refl1_valid = refl1_enable && !src1_moving && segment_intersects(src1_refl1_x, src1_refl1_y, pix_x_out, pix_y_out, refl1_x0, refl1_y0, refl1_x1, refl1_y1);
wire src2_refl1_valid = refl1_enable && !src2_moving && segment_intersects(src2_refl1_x, src2_refl1_y, pix_x_out, pix_y_out, refl1_x0, refl1_y0, refl1_x1, refl1_y1);

wire signed [7:0] contrib_dir1 = wave_contrib(dist_dir1, current_time, src1_gain, src1_phase, src1_enable && !src1_blocked, 4'd8, 1'b0);
wire signed [7:0] contrib_dir2 = wave_contrib(dist_dir2, current_time, src2_gain, src2_phase, src2_enable && !src2_blocked, 4'd8, 1'b0);

wire signed [7:0] contrib_r10 = wave_contrib(dist_r10, current_time, src1_gain, src1_phase, src1_enable && src1_refl0_valid, refl0_gain, refl0_invert);
wire signed [7:0] contrib_r20 = wave_contrib(dist_r20, current_time, src2_gain, src2_phase, src2_enable && src2_refl0_valid, refl0_gain, refl0_invert);
wire signed [7:0] contrib_r11 = wave_contrib(dist_r11, current_time, src1_gain, src1_phase, src1_enable && src1_refl1_valid, refl1_gain, refl1_invert);
wire signed [7:0] contrib_r21 = wave_contrib(dist_r21, current_time, src2_gain, src2_phase, src2_enable && src2_refl1_valid, refl1_gain, refl1_invert);

wire signed [11:0] field_sum = {{4{contrib_dir1[7]}}, contrib_dir1} +
                               {{4{contrib_dir2[7]}}, contrib_dir2} +
                               {{4{contrib_r10[7]}},  contrib_r10}  +
                               {{4{contrib_r20[7]}},  contrib_r20}  +
                               {{4{contrib_r11[7]}},  contrib_r11}  +
                               {{4{contrib_r21[7]}},  contrib_r21};
wire signed [5:0] field_clamped = clamp6(field_sum);

wire is_pos = (field_clamped > 0);
wire is_neg = (field_clamped < 0);
wire [5:0] abs_field = is_neg ? -field_clamped : field_clamped;
wire [3:0] abs_amp = (abs_field > 6'd7) ? 4'd7 : abs_field[3:0];

wire [7:0] r_wave = is_pos ? (abs_amp * 8'd32) :
                    is_neg ? (abs_amp * 8'd16) :
                             8'd0;

wire [7:0] g_wave = is_pos ? (abs_amp * 8'd20) : 8'd0;
wire [7:0] b_wave = is_neg ? (abs_amp * 8'd32) : 8'd0;

wire show_wave = (abs_amp != 0);
wire is_grid_line = (x_out[5:0] == 6'd0) || (y_out[5:0] == 6'd0);

wire [7:0] r_calc = show_wave ? r_wave : (is_grid_line ? 8'hFF : 8'h00);
wire [7:0] g_calc = show_wave ? g_wave : 8'h00;
wire [7:0] b_calc = show_wave ? b_wave : 8'h00;

wire fifo_wr_en = cordic_output_valid && !fifo_full;
wire ready;
wire fifo_rd_en = fifo_valid && ready;

wire        fifo_sof_out = pixel_fifo[fifo_rd_ptr][25];
wire        fifo_eol_out = pixel_fifo[fifo_rd_ptr][24];
wire [7:0]  fifo_r_out   = pixel_fifo[fifo_rd_ptr][23:16];
wire [7:0]  fifo_g_out   = pixel_fifo[fifo_rd_ptr][15:8];
wire [7:0]  fifo_b_out   = pixel_fifo[fifo_rd_ptr][7:0];

always @(posedge out_stream_aclk) begin
    if (!periph_resetn) begin
        fifo_wr_ptr <= {FIFO_AWIDTH{1'b0}};
        fifo_rd_ptr <= {FIFO_AWIDTH{1'b0}};
        fifo_count  <= {(FIFO_AWIDTH+1){1'b0}};
    end else begin
        if (fifo_wr_en) begin
            pixel_fifo[fifo_wr_ptr] <= {
                output_first,
                output_lastx,
                r_calc,
                g_calc,
                b_calc
            };

            fifo_wr_ptr <= fifo_wr_ptr + 1'b1;
        end

        if (fifo_rd_en) begin
            fifo_rd_ptr <= fifo_rd_ptr + 1'b1;
        end

        case ({fifo_wr_en, fifo_rd_en})
            2'b10: fifo_count <= fifo_count + 1'b1;
            2'b01: fifo_count <= fifo_count - 1'b1;
            default: fifo_count <= fifo_count;
        endcase
    end
end

packer pixel_packer (
    .aclk              (out_stream_aclk),
    .aresetn           (periph_resetn),
    .r                 (fifo_r_out),
    .g                 (fifo_g_out),
    .b                 (fifo_b_out),
    .eol               (fifo_eol_out),
    .in_stream_ready   (ready),
    .valid             (fifo_valid),
    .sof               (fifo_sof_out),
    .out_stream_tdata  (out_stream_tdata),
    .out_stream_tkeep  (out_stream_tkeep),
    .out_stream_tlast  (out_stream_tlast),
    .out_stream_tready (out_stream_tready),
    .out_stream_tvalid (out_stream_tvalid),
    .out_stream_tuser  (out_stream_tuser)
);

endmodule