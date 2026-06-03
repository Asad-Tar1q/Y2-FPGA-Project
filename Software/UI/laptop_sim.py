import socket
import struct
import threading
import time

import numpy as np
import pygame
import matplotlib.cm as cm

from numba import cuda

PYNQ_IP = "192.168.2.99"
PYNQ_PORT = 5000
WIDTH, HEIGHT = 640, 480
FRAME_BYTES = WIDTH * HEIGHT  # uint8 grayscale/field values

# --- Step 3: Pre-compute viridis LUT (256 x 3, uint8)
_cmap = cm.get_cmap("viridis", 256)
LUT = (_cmap(np.arange(256))[:, :3] * 255).astype(np.uint8)  # shape (256, 3)

# --- Shared frame slot ---
_frame_lock = threading.Lock()
_latest_frame: np.ndarray | None = None  # 640x480 uint8


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes from sock, handling TCP fragmentation."""
    buf = bytearray(n)
    view = memoryview(buf)
    received = 0
    while received < n:
        chunk = sock.recv_into(view[received:], n - received)
        if not chunk:
            raise ConnectionError("Socket closed by remote")
        received += chunk
    return bytes(buf)


def _receive_thread(sock: socket.socket) -> None:
    """Background thread: receive frames and write into the shared slot."""
    global _latest_frame
    while True:
        try:
            # Step 2: 4-byte header encodes payload length
            header = _recv_exactly(sock, 4)
            payload_len = struct.unpack(">I", header)[0]
            raw = _recv_exactly(sock, payload_len)
        except (ConnectionError, OSError):
            break

        frame = np.frombuffer(raw, dtype=np.uint8).reshape((HEIGHT, WIDTH))

        with _frame_lock:
            _latest_frame = frame  # overwrite; drop-on-busy is implicit


@cuda.jit
def GPU_render(frame_flat, lut, rgb_out):
    #make sure threads run in parallel
    idx = cuda.grid(1)
    if idx < frame_flat.size:
        val = frame_flat[idx]
        rgb_out[idx, 0] = lut[val, 0]
        rgb_out[idx, 1] = lut[val, 1]
        rgb_out[idx, 2] = lut[val, 2]

def main() -> None:
    global _latest_frame

    # --- Step 1: TCP connect ---
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((PYNQ_IP, PYNQ_PORT))
    print(f"Connected to {PYNQ_IP}:{PYNQ_PORT}")

    # --- Step 2: Spawn receive thread ---
    t = threading.Thread(target=_receive_thread, args=(sock,), daemon=True)
    t.start()

    # --- Step 4: Pygame display loop ---
    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption("PYNQ Frame Viewer")
    clock = pygame.time.Clock()

    #upload LUT to GPU once
    d_lut = cuda.to_device(LUT)
    #create an output buffer
    d_rgb_flat = cuda.device_array((HEIGHT * WIDTH, 3), dtype=np.uint8)
    THREADS = 256
    BLOCKS = (WIDTH * HEIGHT + THREADS - 1) // THREADS

    # Step 6: throughput tracking
    display_frames = 0
    fps_timer = time.monotonic()

    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                sock.close()
                return

        # Grab latest frame
        with _frame_lock:
            frame = _latest_frame
            if frame is not None:
                frame = frame.copy()  # release lock immediately after copy

        
        
        if frame is not None:
            d_frame = cuda.to_device(frame.ravel())
            GPU_render[BLOCKS, THREADS](d_frame, d_lut, d_rgb_flat)
            #do the ouput on cpu
            rgb = d_rgb_flat.copy_to_host().reshape((HEIGHT, WIDTH, 3))
            surface = pygame.surfarray.make_surface(rgb.transpose(1, 0, 2))
            screen.blit(surface, (0, 0))
            pygame.display.flip()
            display_frames += 1

        # Step 6: print fps every second
        now = time.monotonic()
        elapsed = now - fps_timer
        if elapsed >= 1.0:
            print(f"Display FPS: {display_frames / elapsed:.1f}")
            display_frames = 0
            fps_timer = now

        #cap at 60 fps - the server bottlenecks to ~70fps
        clock.tick(60)


if __name__ == "__main__":
    main()
