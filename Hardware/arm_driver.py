from pynq import Overlay
from pynq.lib.video import *
import PIL.Image

overlay = Overlay("/home/xilinx/jupyter_notebooks/EMWaves/test_overlays/base.bit")

pixgen = overlay.pixel_generator_0
imgen_vdma = overlay.video.axi_vdma_0.readchannel
hdmi_out = overlay.video.hdmi_out

hdmi_out._vdma = overlay.video.axi_vdma

videoMode = VideoMode(640, 480, 24)
imgen_vdma.mode = videoMode
hdmi_out.configure(videoMode)

# regfile[1] -> Offset 0x04
# regfile[2] -> Offset 0x08
# regfile[3] -> Offset 0x0C
# regfile[4] -> Offset 0x10

src1_x, src1_y = 200, 240
src2_x, src2_y = 440, 240

#im shifting the y vals up by 16 and then combining w the x vals to pack coords into one reg rn
#the FFFF bit is just a mask
src1_packed = (src1_y << 16) | (src1_x & 0xFFFF)
src2_packed = (src2_y << 16) | (src2_x & 0xFFFF)

pixgen.write(0x04, src1_packed)
pixgen.write(0x08, src2_packed)

imgen_vdma.start()
hdmi_out.start()

print("Streaming started.")

try:
    for i in range(100000):
        
        # Write the current frame number to regfile[0] to animate
        # Offset 0x00 corresponds to regfile[0]
        pixgen.write(0x00, i)
        
        # Read the generated frame from your custom IP
        frame = imgen_vdma.readframe()
        
        # Write that frame to the physical HDMI output
        hdmi_out.writeframe(frame)

except KeyboardInterrupt:
    print("Simulation stopped manually.")

finally:
    # Safely shut down the hardware when the loop finishes
    imgen_vdma.stop()
    hdmi_out.close()
    print("Hardware shut down safely.")