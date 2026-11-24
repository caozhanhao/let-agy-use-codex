from __future__ import annotations

import base64
import json
import os
import select
import shutil
import socket
import struct
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse


CLIENT_NAME = "let-agy-use-codex"
CLIENT_TITLE = "Let AGY Use Codex"
CLIENT_VERSION = "0.1.0"


class CodexBridgeError(RuntimeError):
    """Raised when connecting to or talking with Codex app-server fails."""


class CodexRpcError(CodexBridgeError):
    """Raised when Codex app-server returns a JSON-RPC error object."""

    def __init__(self, error: Dict[str, Any]):
        self.error = error
        code = error.get("code")
        message = error.get("message", "Unknown JSON-RPC error")
        data = error.get("data")
        detail = f" ({json.dumps(data, ensure_ascii=False)})" if data is not None else ""
        super().__init__(f"Codex RPC error {code}: {message}{detail}")


def find_codex_binary() -> str:
    """Locate the `codex` CLI binary even when GUI apps have a minimal PATH."""
    env_bin = os.environ.get("CODEX_BIN")
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin

    found = shutil.which("codex")
    if found:
        return found

    home = os.path.expanduser("~")
    candidates = [
        "/opt/homebrew/bin/codex",
        "/usr/local/bin/codex",
        os.path.join(home, ".local", "bin", "codex"),
        os.path.join(home, ".npm-global", "bin", "codex"),
        os.path.join(home, ".volta", "bin", "codex"),
        os.path.join(home, ".cargo", "bin", "codex"),
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    raise CodexBridgeError(
        "Could not find the `codex` binary. Install Codex CLI or set CODEX_BIN."
    )


class _FdTransport:
    """Thin byte transport over either a socket or subprocess stdio pipes."""

    def __init__(
        self,
        read_fd: int,
        write_fd: int,
        closer: Callable[[], None],
        eof_context: Callable[[], str],
    ):
        self.read_fd = read_fd
        self.write_fd = write_fd
        self._closer = closer
        self._eof_context = eof_context

    def read_chunk(self, timeout: float) -> bytes:
        r, _, _ = select.select([self.read_fd], [], [], timeout)
        if not r:
            raise TimeoutError("Timed out reading from Codex app-server")
        chunk = os.read(self.read_fd, 65536)
        if not chunk:
            ctx = self._eof_context().strip()
            msg = "Connection to Codex app-server closed unexpectedly"
            if ctx:
                msg = f"{msg}: {ctx}"
            raise CodexBridgeError(msg)
        return chunk

    def write_all(self, data: bytes) -> None:
        view = memoryview(data)
        total = 0
        while total < len(data):
            written = os.write(self.write_fd, view[total:])
            if written <= 0:
                raise CodexBridgeError("Failed to write bytes to Codex app-server")
            total += written

    def close(self) -> None:
        self._closer()


class WebSocketClient:
    """Minimal pure-stdlib RFC 6455 client over a raw byte transport."""

    def __init__(self, transport: _FdTransport):
        self.transport = transport
        self.buf = bytearray()

    def _read_exact(self, n: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while len(self.buf) < n:
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError("Timed out waiting for WebSocket frame bytes")
            self.buf.extend(self.transport.read_chunk(rem))
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def handshake(
        self,
        host: str = "localhost",
        path: str = "/",
        extra_headers: Optional[Dict[str, str]] = None,
        timeout: float = 10.0,
    ) -> str:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if extra_headers:
            for k, v in extra_headers.items():
                lines.append(f"{k}: {v}")
        raw_req = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
        self.transport.write_all(raw_req)

        deadline = time.monotonic() + timeout
        while b"\r\n\r\n" not in self.buf:
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError("Timed out during WebSocket handshake")
            self.buf.extend(self.transport.read_chunk(rem))

        idx = self.buf.index(b"\r\n\r\n")
        header_text = bytes(self.buf[:idx]).decode("utf-8", errors="replace")
        del self.buf[: idx + 4]
        status_line = header_text.splitlines()[0] if header_text else ""
        if "101" not in status_line:
            raise CodexBridgeError(
                f"Codex app-server WebSocket handshake failed: {status_line!r}"
            )
        return status_line

    def send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self.transport.write_all(bytes(header) + masked)

    def send_json(self, obj: Dict[str, Any]) -> None:
        encoded = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_frame(encoded, opcode=0x1)

    def recv_text(self, timeout: float = 20.0) -> str:
        fragments = bytearray()
        deadline = time.monotonic() + timeout
        while True:
            rem = max(0.05, deadline - time.monotonic())
            b1, b2 = self._read_exact(2, rem)
            fin = (b1 & 0x80) != 0
            opcode = b1 & 0x0F
            masked = (b2 & 0x80) != 0
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2, rem))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8, rem))[0]
            mask = self._read_exact(4, rem) if masked else None
            data = self._read_exact(length, rem) if length else b""
            if mask:
                data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))

            if opcode == 0x9:  # Ping
                self.send_frame(data, opcode=0xA)
                continue
            if opcode == 0xA:  # Pong
                continue
            if opcode == 0x8:  # Close
                raise CodexBridgeError("Codex app-server closed the WebSocket")
            fragments.extend(data)
            if fin:
                return fragments.decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self.send_frame(b"", opcode=0x8)
        except Exception:
            pass
        self.transport.close()


def _open_proxy_transport(sock_path: Optional[str] = None) -> Tuple[_FdTransport, str, str, Dict[str, str]]:
    codex_bin = find_codex_binary()
    cmd = [codex_bin, "app-server", "proxy"]
    if sock_path:
        cmd.extend(["--sock", sock_path])
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

    def _eof_ctx() -> str:
        try:
            err = proc.stderr.read().decode("utf-8", errors="replace")
        except Exception:
            err = ""
        hint = ""
        if "No such file or directory" in err or "Connection refused" in err:
            hint = " (Is Codex running? Try `codex app-server daemon start` or open Codex.)"
        return f"rc={proc.poll()} {err.strip()}{hint}".strip()

    def _close() -> None:
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except Exception:
                pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()

    transport = _FdTransport(
        read_fd=proc.stdout.fileno(),
        write_fd=proc.stdin.fileno(),
        closer=_close,
        eof_context=_eof_ctx,
    )
    return transport, "localhost", "/", {}


def _open_remote_transport(
    remote_url: str, auth_token: Optional[str] = None
) -> Tuple[_FdTransport, str, str, Dict[str, str]]:
    parsed = urlparse(remote_url)
    scheme = parsed.scheme.lower()
    headers: Dict[str, str] = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    if scheme == "unix":
        sock_path = parsed.path or ""
        if not sock_path:
            return _open_proxy_transport(None)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(sock_path)
        host = "localhost"
        path = "/"
    elif scheme == "ws":
        host_name = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        sock = socket.create_connection((host_name, port), timeout=10.0)
        host = f"{host_name}:{port}"
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
    else:
        raise CodexBridgeError(
            f"Unsupported remote URL scheme {scheme!r}; use unix://PATH, ws://HOST:PORT, or omit for local daemon proxy."
        )

    fd = sock.fileno()

    def _close() -> None:
        try:
            sock.close()
        except Exception:
            pass

    transport = _FdTransport(
        read_fd=fd,
        write_fd=fd,
        closer=_close,
        eof_context=lambda: f"remote={remote_url}",
    )
    return transport, host, path, headers


class CodexAppClient:
    """Context-managed JSON-RPC 2.0 client for the Codex app-server daemon."""

    def __init__(
        self,
        sock_path: Optional[str] = None,
        remote_url: Optional[str] = None,
        auth_token: Optional[str] = None,
        rpc_timeout: float = 25.0,
    ):
        self.sock_path = sock_path or os.environ.get("CODEX_APP_SERVER_SOCK")
        self.remote_url = remote_url or os.environ.get("CODEX_REMOTE_URL")
        self.auth_token = auth_token or os.environ.get("CODEX_REMOTE_AUTH_TOKEN")
        self.rpc_timeout = rpc_timeout
        self.ws: Optional[WebSocketClient] = None
        self.server_info: Dict[str, Any] = {}
        self.handshake_status: str = ""
        self._next_id = 0
        self.notifications: List[Dict[str, Any]] = []

    def __enter__(self) -> "CodexAppClient":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def connect(self) -> None:
        if self.remote_url:
            transport, host, path, headers = _open_remote_transport(
                self.remote_url, self.auth_token
            )
        else:
            transport, host, path, headers = _open_proxy_transport(self.sock_path)

        self.ws = WebSocketClient(transport)
        try:
            self.handshake_status = self.ws.handshake(
                host=host, path=path, extra_headers=headers, timeout=10.0
            )
            self.server_info = self.call(
                "initialize",
                {
                    "clientInfo": {
                        "name": CLIENT_NAME,
                        "title": CLIENT_TITLE,
                        "version": CLIENT_VERSION,
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            self.notify("initialized")
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self.ws is not None:
            ws = self.ws
            self.ws = None
            ws.close()

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        if self.ws is None:
            raise CodexBridgeError("Client is not connected")
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.ws.send_json(msg)

    def call(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        if self.ws is None:
            raise CodexBridgeError("Client is not connected")
        self._next_id += 1
        req_id = self._next_id
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        self.ws.send_json(payload)

        deadline = time.monotonic() + (timeout or self.rpc_timeout)
        while True:
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError(f"Timed out waiting for response to {method}")
            raw = self.ws.recv_text(timeout=rem)
            msg = json.loads(raw)
            if msg.get("id") == req_id:
                if "error" in msg and msg["error"] is not None:
                    raise CodexRpcError(msg["error"])
                return msg.get("result")
            # Buffer server notifications; ignore unrelated messages safely.
            if "method" in msg and "id" not in msg:
                self.notifications.append(msg)
