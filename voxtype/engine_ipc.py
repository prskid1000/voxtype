"""Length-prefixed socket framing for the engine worker IPC.

Wire format (one frame):

    [4 bytes big-endian header length H]
    [H bytes UTF-8 JSON header]
    [header["nbytes"] bytes raw binary payload]   # 0 when absent

The JSON header carries the op + scalar fields; the binary payload carries
bulk bytes (PCM in, WAV/PCM out) so audio rides the wire without base64.

Requests and responses use the same frame shape. A streaming response is
several frames: zero or more `{"chunk": true, "nbytes": k}` payload frames
followed by a terminal `{"end": true}` (or `{"error": ...}`) frame.

ASCII-only log strings (the worker shares the cp1252 console caveat — see
debug_log)."""
from __future__ import annotations

import json
import socket
import struct
from typing import Any

_HDR = struct.Struct("!I")


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly `n` bytes or return b"" if the peer closed early."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return b""
        buf.extend(chunk)
    return bytes(buf)


def send_frame(sock: socket.socket, header: dict[str, Any],
               payload: bytes = b"") -> None:
    """Serialize and send one frame. `nbytes` in the header is set to the
    payload length so the receiver knows how much binary to read."""
    header = dict(header)
    header["nbytes"] = len(payload)
    hjson = json.dumps(header).encode("utf-8")
    sock.sendall(_HDR.pack(len(hjson)) + hjson + payload)


def recv_frame(sock: socket.socket) -> tuple[dict[str, Any] | None, bytes]:
    """Read one frame. Returns (header, payload); header is None when the
    connection closed cleanly between frames."""
    raw = recv_exact(sock, _HDR.size)
    if not raw:
        return None, b""
    (hlen,) = _HDR.unpack(raw)
    hjson = recv_exact(sock, hlen)
    if not hjson:
        return None, b""
    header = json.loads(hjson.decode("utf-8"))
    nbytes = int(header.get("nbytes", 0))
    payload = recv_exact(sock, nbytes) if nbytes else b""
    return header, payload
