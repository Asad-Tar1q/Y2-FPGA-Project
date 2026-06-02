import socket
import struct
import time

import numpy as np

HOST = "0.0.0.0"
PORT = 5050
WIDTH, HEIGHT = 640, 480
FRAME_BYTES = WIDTH * HEIGHT


def main() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(1)
    print(f"Listening on {HOST}:{PORT}")

    while True:
        conn, addr = server.accept()
        print(f"Client connected: {addr}")

        #throughput tracking
        frames_sent = 0
        fps_timer = time.monotonic()

        try:
            while True:
                #generate fake frame
                frame = np.random.randint(0, 255, (HEIGHT, WIDTH), dtype=np.uint8)
                payload = frame.tobytes()

                #4-byte big-endian length header
                header = struct.pack(">I", FRAME_BYTES)

                #send frames
                conn.sendall(header + payload)

                frames_sent += 1

                #print fps every second
                now = time.monotonic()
                elapsed = now - fps_timer
                if elapsed >= 1.0:
                    print(f"Send FPS: {frames_sent / elapsed:.1f}")
                    frames_sent = 0
                    fps_timer = now

        except (BrokenPipeError, ConnectionResetError):
            print(f"Client disconnected: {addr}")
        finally:
            conn.close()


if __name__ == "__main__":
    main()
