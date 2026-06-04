import socket
import struct
import time
import numpy as np
from pynq import Overlay, allocate

#set up the hardware: /home/xilinx/jupyter_notebooks/......
ol  = Overlay('/home/xilinx/jupyter_notebooks/UITest/ui_benchmark.bit')
#print(ol.ip_dict.keys()) 

dma = ol.axi_dma_0
#print(type(dma)) 

WIDTH, HEIGHT = 640, 480
FRAME_BYTES   = WIDTH * HEIGHT

#two buffers so we can DMA into one while sending the other
buf_a = allocate(shape=(HEIGHT, WIDTH), dtype=np.uint8)
buf_b = allocate(shape=(HEIGHT, WIDTH), dtype=np.uint8)

#set TCP server parameters
HOST = "0.0.0.0"
PORT = 5000

def main() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(1)
    print(f"Listening on {HOST}:{PORT}")

    while True:
        conn, addr = server.accept()
        print(f"Client connected: {addr}")

        frames_sent = 0
        fps_timer   = time.monotonic()

        # Timing breakdown accumulators
        dma_total  = 0.0
        tcp_total  = 0.0

        try:
            # Prime the pipeline: start first DMA transfer
            active_buf = buf_a
            next_buf   = buf_b
            dma.recvchannel.transfer(active_buf)

            while True:
                #wait for the current DMA transfer to complete
                t0 = time.perf_counter()
                dma.recvchannel.wait()
                t1 = time.perf_counter()

                #immediately re-arm DMA on the other bufferso that the hardware runs while we send
                dma.recvchannel.transfer(next_buf)

                #Copy frame out (fast — just a numpy view)
                frame = bytes(active_buf)

                #Send: 4-byte big-endian length header + payload
                t2 = time.perf_counter()
                header = struct.pack(">I", FRAME_BYTES)
                conn.sendall(header + frame)
                t3 = time.perf_counter()

                dma_total += (t1 - t0)
                tcp_total += (t3 - t2)

                #swap the buffers
                active_buf, next_buf = next_buf, active_buf
                frames_sent += 1

                #print stats every second
                now     = time.monotonic()
                elapsed = now - fps_timer
                if elapsed >= 1.0:
                    fps = frames_sent / elapsed
                    print(f"FPS: {fps:.1f}  |  "
                          f"DMA: {dma_total/frames_sent*1e3:.2f} ms/frame  |  "
                          f"TCP: {tcp_total/frames_sent*1e3:.2f} ms/frame")
                    frames_sent = 0
                    fps_timer   = now
                    dma_total   = 0.0
                    tcp_total   = 0.0

        except (BrokenPipeError, ConnectionResetError):
            print(f"Client disconnected: {addr}")
        finally:
            conn.close()

if __name__ == "__main__":
    main()