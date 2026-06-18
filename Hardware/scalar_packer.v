module scalar_packer #(
    parameter X_SIZE = 640,
    parameter Y_SIZE = 480
)(
    input             clk,
    input             rst_n,

    input      [7:0]  scalar_in,
    input             pixel_valid,
    output            pixel_ready,

    output reg [31:0] tdata,
    output     [3:0]  tkeep,
    output reg        tvalid,
    input             tready,
    output reg        tlast,
    output reg        tuser
);

localparam TOTAL_PIXELS = X_SIZE * Y_SIZE;
localparam PIXEL_COUNT_WIDTH = $clog2(TOTAL_PIXELS);

reg [7:0] pack0;
reg [7:0] pack1;
reg [7:0] pack2;
reg [1:0] phase;

reg [PIXEL_COUNT_WIDTH-1:0] pixel_count;

assign tkeep = 4'hF;
assign pixel_ready = (!tvalid) || tready;

wire take_pixel = pixel_valid && pixel_ready;

always @(posedge clk) begin
    if (!rst_n) begin
        pack0       <= 8'd0;
        pack1       <= 8'd0;
        pack2       <= 8'd0;
        phase       <= 2'd0;
        pixel_count <= {PIXEL_COUNT_WIDTH{1'b0}};

        tdata       <= 32'd0;
        tvalid      <= 1'b0;
        tlast       <= 1'b0;
        tuser       <= 1'b0;
    end else begin
        if (tvalid && !tready) begin
            tdata  <= tdata;
            tvalid <= tvalid;
            tlast  <= tlast;
            tuser  <= tuser;
        end else begin
            tvalid <= 1'b0;
            tlast  <= 1'b0;
            tuser  <= 1'b0;

            if (take_pixel) begin
                case (phase)
                    2'd0: pack0 <= scalar_in;
                    2'd1: pack1 <= scalar_in;
                    2'd2: pack2 <= scalar_in;
                    2'd3: begin
                        tdata  <= {scalar_in, pack2, pack1, pack0};
                        tvalid <= 1'b1;
                        tlast  <= (pixel_count == TOTAL_PIXELS - 1);
                        tuser  <= (pixel_count == 3);
                    end
                endcase

                phase <= phase + 1'b1;

                if (pixel_count == TOTAL_PIXELS - 1)
                    pixel_count <= {PIXEL_COUNT_WIDTH{1'b0}};
                else
                    pixel_count <= pixel_count + 1'b1;
            end
        end
    end
end

endmodule
