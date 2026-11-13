"""A tiny clamd stand-in speaking the INSTREAM protocol over TCP.

Reports the EICAR test string as infected and everything else as clean, like a real
clamd with the standard signatures. Can be switched to answer with an error."""

from __future__ import annotations

import socketserver
import struct
import threading

EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


class FakeClamd:
    def __init__(self) -> None:
        self.scanned: list[bytes] = []
        self.broken = False
        fake = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                command = b""
                while not command.endswith(b"\0"):
                    part = self.request.recv(1)
                    if not part:
                        return
                    command += part
                if command == b"zPING\0":
                    self.request.sendall(b"PONG\0")
                    return
                assert command == b"zINSTREAM\0", command
                data = b""
                while True:
                    size = struct.unpack(">I", self._exactly(4))[0]
                    if size == 0:
                        break
                    data += self._exactly(size)
                fake.scanned.append(data)
                if fake.broken:
                    reply = b"INSTREAM size limit exceeded. ERROR\0"
                elif EICAR in data:
                    reply = b"stream: Eicar-Test-Signature FOUND\0"
                else:
                    reply = b"stream: OK\0"
                self.request.sendall(reply)

            def _exactly(self, n: int) -> bytes:
                buf = b""
                while len(buf) < n:
                    part = self.request.recv(n - len(buf))
                    if not part:
                        raise ConnectionError("closed")
                    buf += part
                return buf

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
