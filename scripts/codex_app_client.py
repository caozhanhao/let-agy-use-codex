from __future__ import annotations

import base64
import collections
import hashlib
import ipaddress
import json
import os
import select
import shutil
import socket
import ssl
import struct
import subprocess
import threading
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlparse


CLIENT_NAME = "let-agy-use-codex"
CLIENT_TITLE = "Let AGY Use Codex"
CLIENT_VERSION = "0.1.0"

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_WS_HANDSHAKE_BYTES = 65536
MAX_WS_FRAME_BYTES = 32 * 1024 * 1024
MAX_WS_MESSAGE_BYTES = 64 * 1024 * 1024
MAX_STDERR_BYTES = 16384
MAX_NOTIFICATIONS = 256
MAX_PENDING_SERVER_REQUESTS = 64

# Replayable human-in-the-loop server requests that should remain available for
# Codex Desktop / IDE / CLI UIs rather than being rejected or answered by this bridge.
HUMAN_APPROVAL_SERVER_REQUEST_METHODS = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
        "item/tool/requestUserInput",
        "mcpServer/elicitation/request",
        "applyPatchApproval",
        "execCommandApproval",
    }
)


class CodexBridgeError(RuntimeError):
    """Raised when connecting to or talking with Codex app-server fails."""


class CodexCancelledError(CodexBridgeError):
    """Raised when an in-flight operation is cancelled by the MCP host."""


class CodexRpcError(CodexBridgeError):
    """Raised when Codex app-server returns a JSON-RPC error object."""

    def __init__(self, error: Dict[str, Any]):
        self.error = error
        code = error.get("code")
        message = error.get("message", "Unknown JSON-RPC error")
        data = error.get("data")
        detail = f" ({json.dumps(data, ensure_ascii=False)})" if data is not None else ""
        super().__init__(f"Codex RPC error {code}: {message}{detail}")


def _check_cancelled(cancel_event: Optional[threading.Event]) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise CodexCancelledError("Operation cancelled by client")


def _is_loopback_host(host: str) -> bool:
    cleaned = host.strip().lower()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    if cleaned == "localhost" or cleaned.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(cleaned).is_loopback
    except ValueError:
        return False


def _format_host_header(host: str, port: int, default_port: int) -> str:
    formatted_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if port == default_port:
        return formatted_host
    return f"{formatted_host}:{port}"


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


class _SocketTransport:
    """Non-blocking byte transport over a plain socket or ssl.SSLSocket."""

    def __init__(
        self,
        sock: socket.socket,
        eof_context: Callable[[], str],
    ):
        self.sock = sock
        self._eof_context = eof_context
        self.sock.setblocking(False)

    def read_chunk(
        self,
        timeout: float,
        cancel_event: Optional[threading.Event] = None,
    ) -> bytes:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            _check_cancelled(cancel_event)
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError("Timed out reading from Codex app-server")

            need_select = True
            if isinstance(self.sock, ssl.SSLSocket) and self.sock.pending() > 0:
                need_select = False

            if need_select:
                wait = min(rem, 0.25) if cancel_event is not None else rem
                r, _, _ = select.select([self.sock], [], [], wait)
                if not r:
                    continue

            try:
                chunk = self.sock.recv(65536)
            except ssl.SSLWantReadError:
                continue
            except ssl.SSLWantWriteError:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    raise TimeoutError("Timed out reading from Codex app-server")
                wait = min(rem, 0.25) if cancel_event is not None else rem
                select.select([], [self.sock], [], wait)
                continue
            except BlockingIOError:
                continue
            except OSError as exc:
                ctx = self._eof_context().strip()
                msg = f"Connection to Codex app-server failed: {exc}"
                if ctx:
                    msg = f"{msg} ({ctx})"
                raise CodexBridgeError(msg) from exc

            if not chunk:
                ctx = self._eof_context().strip()
                msg = "Connection to Codex app-server closed unexpectedly"
                if ctx:
                    msg = f"{msg}: {ctx}"
                raise CodexBridgeError(msg)
            return chunk

    def write_all(
        self,
        data: bytes,
        timeout: float = 15.0,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        view = memoryview(data)
        total = 0
        while total < len(data):
            _check_cancelled(cancel_event)
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError("Timed out writing to Codex app-server")

            wait = min(rem, 0.25) if cancel_event is not None else rem
            _, w, _ = select.select([], [self.sock], [], wait)
            if not w:
                continue

            try:
                written = self.sock.send(view[total:])
            except (BlockingIOError, ssl.SSLWantWriteError):
                continue
            except ssl.SSLWantReadError:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    raise TimeoutError("Timed out writing to Codex app-server")
                wait = min(rem, 0.25) if cancel_event is not None else rem
                select.select([self.sock], [], [], wait)
                continue
            except OSError as exc:
                raise CodexBridgeError(
                    f"Failed to write bytes to Codex app-server: {exc}"
                ) from exc

            if written <= 0:
                raise CodexBridgeError("Failed to write bytes to Codex app-server")
            total += written

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


class _PipeTransport:
    """Non-blocking byte transport over a subprocess stdin/stdout pipe pair."""

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
        os.set_blocking(self.read_fd, False)
        os.set_blocking(self.write_fd, False)

    def read_chunk(
        self,
        timeout: float,
        cancel_event: Optional[threading.Event] = None,
    ) -> bytes:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            _check_cancelled(cancel_event)
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError("Timed out reading from Codex app-server")

            wait = min(rem, 0.25) if cancel_event is not None else rem
            r, _, _ = select.select([self.read_fd], [], [], wait)
            if not r:
                continue

            try:
                chunk = os.read(self.read_fd, 65536)
            except BlockingIOError:
                continue
            except OSError as exc:
                ctx = self._eof_context().strip()
                msg = f"Failed reading from Codex app-server: {exc}"
                if ctx:
                    msg = f"{msg}: {ctx}"
                raise CodexBridgeError(msg) from exc

            if not chunk:
                ctx = self._eof_context().strip()
                msg = "Connection to Codex app-server closed unexpectedly"
                if ctx:
                    msg = f"{msg}: {ctx}"
                raise CodexBridgeError(msg)
            return chunk

    def write_all(
        self,
        data: bytes,
        timeout: float = 15.0,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        view = memoryview(data)
        total = 0
        while total < len(data):
            _check_cancelled(cancel_event)
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError("Timed out writing to Codex app-server")

            wait = min(rem, 0.25) if cancel_event is not None else rem
            _, w, _ = select.select([], [self.write_fd], [], wait)
            if not w:
                continue

            try:
                written = os.write(self.write_fd, view[total:])
            except BlockingIOError:
                continue
            except OSError as exc:
                ctx = self._eof_context().strip()
                msg = f"Failed to write bytes to Codex app-server: {exc}"
                if ctx:
                    msg = f"{msg}: {ctx}"
                raise CodexBridgeError(msg) from exc

            if written <= 0:
                raise CodexBridgeError("Failed to write bytes to Codex app-server")
            total += written

    def close(self) -> None:
        self._closer()


# Backward-compatible alias for any callers/tests importing `_FdTransport`.
_FdTransport = _PipeTransport


class WebSocketClient:
    """Minimal pure-stdlib RFC 6455 client over a raw byte transport."""

    def __init__(self, transport: Any):
        self.transport = transport
        self.buf = bytearray()

    def _read_chunk(
        self,
        timeout: float,
        cancel_event: Optional[threading.Event] = None,
    ) -> bytes:
        try:
            return self.transport.read_chunk(timeout, cancel_event=cancel_event)
        except TypeError:
            return self.transport.read_chunk(timeout)

    def _write_all(
        self,
        data: bytes,
        timeout: float = 15.0,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        try:
            self.transport.write_all(data, timeout=timeout, cancel_event=cancel_event)
        except TypeError:
            self.transport.write_all(data)

    def _read_exact_until(
        self,
        n: int,
        deadline: float,
        cancel_event: Optional[threading.Event] = None,
    ) -> bytes:
        while len(self.buf) < n:
            _check_cancelled(cancel_event)
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError("Timed out waiting for WebSocket frame bytes")
            self.buf.extend(self._read_chunk(rem, cancel_event=cancel_event))
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def handshake(
        self,
        host: str = "localhost",
        path: str = "/",
        extra_headers: Optional[Dict[str, str]] = None,
        timeout: float = 10.0,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        deadline = time.monotonic() + max(0.0, timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        expected_accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
        ).decode("ascii")

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

        rem = deadline - time.monotonic()
        if rem <= 0:
            raise TimeoutError("Timed out during WebSocket handshake")
        self._write_all(raw_req, timeout=rem, cancel_event=cancel_event)

        while b"\r\n\r\n" not in self.buf:
            _check_cancelled(cancel_event)
            if len(self.buf) > MAX_WS_HANDSHAKE_BYTES:
                raise CodexBridgeError(
                    "Codex app-server WebSocket handshake response exceeded size limit"
                )
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError("Timed out during WebSocket handshake")
            self.buf.extend(self._read_chunk(rem, cancel_event=cancel_event))

        idx = self.buf.index(b"\r\n\r\n")
        if idx > MAX_WS_HANDSHAKE_BYTES:
            raise CodexBridgeError(
                "Codex app-server WebSocket handshake response exceeded size limit"
            )
        header_text = bytes(self.buf[:idx]).decode("utf-8", errors="replace")
        del self.buf[: idx + 4]

        header_lines = header_text.split("\r\n")
        status_line = header_lines[0] if header_lines else ""
        status_parts = status_line.split(" ", 2)
        if (
            len(status_parts) < 2
            or not status_parts[0].upper().startswith("HTTP/")
            or status_parts[1] != "101"
        ):
            raise CodexBridgeError(
                f"Codex app-server WebSocket handshake failed: {status_line!r}"
            )

        headers: Dict[str, List[str]] = {}
        for line in header_lines[1:]:
            if not line or ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers.setdefault(name.strip().lower(), []).append(value.strip())

        upgrade_tokens = {
            tok.strip().lower()
            for val in headers.get("upgrade", [])
            for tok in val.split(",")
            if tok.strip()
        }
        if "websocket" not in upgrade_tokens:
            raise CodexBridgeError(
                "Codex app-server WebSocket handshake missing `Upgrade: websocket` header"
            )

        connection_tokens = {
            tok.strip().lower()
            for val in headers.get("connection", [])
            for tok in val.split(",")
            if tok.strip()
        }
        if "upgrade" not in connection_tokens:
            raise CodexBridgeError(
                "Codex app-server WebSocket handshake missing `Connection: Upgrade` header"
            )

        accept_vals = headers.get("sec-websocket-accept", [])
        if len(accept_vals) != 1 or accept_vals[0] != expected_accept:
            raise CodexBridgeError(
                "Codex app-server WebSocket handshake failed `Sec-WebSocket-Accept` verification"
            )

        return status_line

    def send_frame(
        self,
        payload: bytes,
        opcode: int = 0x1,
        timeout: float = 15.0,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        if opcode >= 0x8 and len(payload) > 125:
            raise CodexBridgeError("WebSocket control frame payload exceeds 125 bytes")
        header = bytearray([0x80 | (opcode & 0x0F)])
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
        self._write_all(bytes(header) + masked, timeout=timeout, cancel_event=cancel_event)

    def send_json(
        self,
        obj: Dict[str, Any],
        timeout: float = 15.0,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        encoded = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_frame(encoded, opcode=0x1, timeout=timeout, cancel_event=cancel_event)

    def recv_text(
        self,
        timeout: float = 20.0,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        fragments = bytearray()
        in_fragment = False
        deadline = time.monotonic() + max(0.0, timeout)

        while True:
            _check_cancelled(cancel_event)
            if time.monotonic() >= deadline:
                raise TimeoutError("Timed out waiting for WebSocket message")

            b1, b2 = self._read_exact_until(2, deadline, cancel_event=cancel_event)
            fin = (b1 & 0x80) != 0
            rsv = b1 & 0x70
            opcode = b1 & 0x0F
            masked = (b2 & 0x80) != 0
            length = b2 & 0x7F

            if rsv != 0:
                raise CodexBridgeError(
                    f"WebSocket protocol error: unexpected RSV bits 0x{rsv:02x}"
                )
            if masked:
                raise CodexBridgeError(
                    "WebSocket protocol error: server-to-client frame must not be masked"
                )

            if length == 126:
                length = struct.unpack(
                    "!H", self._read_exact_until(2, deadline, cancel_event=cancel_event)
                )[0]
            elif length == 127:
                length = struct.unpack(
                    "!Q", self._read_exact_until(8, deadline, cancel_event=cancel_event)
                )[0]
                if length & (1 << 63):
                    raise CodexBridgeError(
                        "WebSocket protocol error: invalid 64-bit payload length"
                    )

            is_control = opcode >= 0x8
            if is_control:
                if not fin:
                    raise CodexBridgeError(
                        "WebSocket protocol error: fragmented control frame"
                    )
                if length > 125:
                    raise CodexBridgeError(
                        "WebSocket protocol error: control frame payload exceeds 125 bytes"
                    )
            else:
                if length > MAX_WS_FRAME_BYTES:
                    raise CodexBridgeError(
                        f"WebSocket frame size {length} exceeds maximum allowed {MAX_WS_FRAME_BYTES} bytes"
                    )

            data = (
                self._read_exact_until(length, deadline, cancel_event=cancel_event)
                if length
                else b""
            )

            if opcode == 0x9:  # Ping
                rem = deadline - time.monotonic()
                if rem <= 0:
                    raise TimeoutError("Timed out while responding to WebSocket ping")
                self.send_frame(data, opcode=0xA, timeout=rem, cancel_event=cancel_event)
                continue
            if opcode == 0xA:  # Pong
                continue
            if opcode == 0x8:  # Close
                close_detail = ""
                if len(data) == 1:
                    raise CodexBridgeError(
                        "WebSocket protocol error: invalid 1-byte close payload"
                    )
                if len(data) >= 2:
                    close_code = struct.unpack("!H", data[:2])[0]
                    reason = data[2:].decode("utf-8", errors="replace")
                    close_detail = f" (code={close_code}" + (
                        f", reason={reason!r})" if reason else ")"
                    )
                try:
                    rem = max(0.2, min(1.0, deadline - time.monotonic()))
                    self.send_frame(data[:2], opcode=0x8, timeout=rem)
                except Exception:
                    pass
                raise CodexBridgeError(
                    f"Codex app-server closed the WebSocket{close_detail}"
                )

            if opcode == 0x2:  # Binary
                raise CodexBridgeError(
                    "WebSocket protocol error: unexpected binary frame (expected UTF-8 JSON text)"
                )
            if opcode == 0x0:  # Continuation
                if not in_fragment:
                    raise CodexBridgeError(
                        "WebSocket protocol error: unexpected continuation frame without initial frame"
                    )
            elif opcode == 0x1:  # Text
                if in_fragment:
                    raise CodexBridgeError(
                        "WebSocket protocol error: new text frame received while previous message is incomplete"
                    )
                if not fin:
                    in_fragment = True
            else:
                raise CodexBridgeError(
                    f"WebSocket protocol error: unsupported opcode 0x{opcode:x}"
                )

            if len(fragments) + len(data) > MAX_WS_MESSAGE_BYTES:
                raise CodexBridgeError(
                    f"WebSocket message size exceeds maximum allowed {MAX_WS_MESSAGE_BYTES} bytes"
                )
            fragments.extend(data)
            if fin:
                try:
                    return fragments.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise CodexBridgeError(
                        f"WebSocket protocol error: invalid UTF-8 payload: {exc}"
                    ) from exc

    def close(self) -> None:
        try:
            self.send_frame(b"", opcode=0x8, timeout=1.0)
        except Exception:
            pass
        self.transport.close()


def _open_proxy_transport(
    sock_path: Optional[str] = None,
) -> Tuple[_PipeTransport, str, str, Dict[str, str]]:
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

    stderr_lock = threading.Lock()
    stderr_buf = bytearray()

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        try:
            while True:
                chunk = proc.stderr.read(4096)
                if not chunk:
                    break
                with stderr_lock:
                    stderr_buf.extend(chunk)
                    if len(stderr_buf) > MAX_STDERR_BYTES:
                        del stderr_buf[: len(stderr_buf) - MAX_STDERR_BYTES]
        except Exception:
            pass

    stderr_thread = threading.Thread(
        target=_drain_stderr, name="codex-proxy-stderr", daemon=True
    )
    stderr_thread.start()

    def _eof_ctx() -> str:
        stderr_thread.join(timeout=0.3)
        with stderr_lock:
            err = bytes(stderr_buf).decode("utf-8", errors="replace")
        hint = ""
        if "No such file or directory" in err or "Connection refused" in err:
            hint = " (Is Codex running? Try `codex app-server daemon start` or open Codex.)"
        return f"rc={proc.poll()} {err.strip()}{hint}".strip()

    def _close() -> None:
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except Exception:
                pass
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=2.0)
                except Exception:
                    pass
            except Exception:
                pass
        else:
            try:
                proc.wait(timeout=0.5)
            except Exception:
                pass
        stderr_thread.join(timeout=0.5)
        try:
            proc.stderr.close()
        except Exception:
            pass

    transport = _PipeTransport(
        read_fd=proc.stdout.fileno(),
        write_fd=proc.stdin.fileno(),
        closer=_close,
        eof_context=_eof_ctx,
    )
    return transport, "localhost", "/", {}


def _open_remote_transport(
    remote_url: str,
    auth_token: Optional[str] = None,
    connect_timeout: float = 10.0,
) -> Tuple[Any, str, str, Dict[str, str]]:
    parsed = urlparse(remote_url)
    scheme = parsed.scheme.lower()
    headers: Dict[str, str] = {}

    if scheme == "unix":
        sock_path = parsed.path or ""
        if not sock_path:
            return _open_proxy_transport(None)
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(connect_timeout)
            sock.connect(sock_path)
        except Exception:
            sock.close()
            raise
        host = "localhost"
        path = "/"
    elif scheme in ("ws", "wss"):
        host_name = parsed.hostname
        if not host_name:
            raise CodexBridgeError(f"Invalid remote URL (missing hostname): {remote_url!r}")
        default_port = 443 if scheme == "wss" else 80
        port = parsed.port or default_port

        if auth_token:
            if scheme == "ws" and not _is_loopback_host(host_name):
                raise CodexBridgeError(
                    f"Refusing to send bearer token over plaintext ws:// to non-loopback host {host_name!r}; "
                    "use wss:// or a loopback ws:// address."
                )
            headers["Authorization"] = f"Bearer {auth_token}"

        raw_sock = socket.create_connection((host_name, port), timeout=connect_timeout)
        if scheme == "wss":
            try:
                ctx = ssl.create_default_context()
                sock = ctx.wrap_socket(raw_sock, server_hostname=host_name)
            except Exception:
                raw_sock.close()
                raise
        else:
            sock = raw_sock

        host = _format_host_header(host_name, port, default_port)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
    else:
        raise CodexBridgeError(
            f"Unsupported remote URL scheme {scheme!r}; use unix://PATH, wss://HOST:PORT, ws://127.0.0.1:PORT, or omit for local daemon proxy."
        )

    transport = _SocketTransport(
        sock=sock,
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
        connect_timeout: float = 10.0,
        cancel_event: Optional[threading.Event] = None,
    ):
        self.sock_path = sock_path or os.environ.get("CODEX_APP_SERVER_SOCK")
        self.remote_url = remote_url or os.environ.get("CODEX_REMOTE_URL")
        self.auth_token = auth_token or os.environ.get("CODEX_REMOTE_AUTH_TOKEN")
        self.rpc_timeout = rpc_timeout
        self.connect_timeout = connect_timeout
        self.cancel_event = cancel_event
        self.ws: Optional[WebSocketClient] = None
        self.server_info: Dict[str, Any] = {}
        self.handshake_status: str = ""
        self._next_id = 0
        self.notifications: Deque[Dict[str, Any]] = collections.deque(
            maxlen=MAX_NOTIFICATIONS
        )
        self.pending_server_requests: Deque[Dict[str, Any]] = collections.deque(
            maxlen=MAX_PENDING_SERVER_REQUESTS
        )

    def __enter__(self) -> "CodexAppClient":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def connect(self, timeout: Optional[float] = None) -> None:
        _check_cancelled(self.cancel_event)
        total_timeout = self.connect_timeout if timeout is None else max(0.1, float(timeout))
        deadline = time.monotonic() + total_timeout

        if self.remote_url:
            transport, host, path, headers = _open_remote_transport(
                self.remote_url,
                self.auth_token,
                connect_timeout=max(0.1, deadline - time.monotonic()),
            )
        else:
            transport, host, path, headers = _open_proxy_transport(self.sock_path)

        self.ws = WebSocketClient(transport)
        try:
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError("Timed out connecting to Codex app-server")
            self.handshake_status = self.ws.handshake(
                host=host,
                path=path,
                extra_headers=headers,
                timeout=rem,
                cancel_event=self.cancel_event,
            )
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError("Timed out initializing Codex app-server")
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
                timeout=rem,
            )
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError("Timed out sending initialized notification")
            self.notify("initialized", timeout=rem)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self.ws is not None:
            ws = self.ws
            self.ws = None
            ws.close()

    def notify(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> None:
        if self.ws is None:
            raise CodexBridgeError("Client is not connected")
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.ws.send_json(
            msg,
            timeout=self.rpc_timeout if timeout is None else timeout,
            cancel_event=self.cancel_event,
        )

    def _handle_server_request(self, msg: Dict[str, Any], remaining: float) -> None:
        req_id = msg.get("id")
        method = str(msg.get("method") or "")
        record: Dict[str, Any] = {
            "id": req_id,
            "method": method,
            "params": msg.get("params"),
        }
        if method in HUMAN_APPROVAL_SERVER_REQUEST_METHODS:
            # Leave human approval / user input requests untouched so Codex Desktop /
            # IDE / CLI UIs can still present and resolve them.
            record["action"] = "deferred_to_human_ui"
            self.pending_server_requests.append(record)
            return

        # Reply with -32601 to client-exclusive requests this bridge cannot fulfill
        # (e.g. dynamic tool calls or token refresh) so Codex does not hang waiting.
        record["action"] = "rejected_unsupported"
        self.pending_server_requests.append(record)
        if self.ws is not None and remaining > 0:
            try:
                self.ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32601,
                            "message": f"Server request {method!r} is not supported by {CLIENT_NAME}",
                        },
                    },
                    timeout=min(remaining, 5.0),
                    cancel_event=self.cancel_event,
                )
            except Exception:
                pass

    def call(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        if self.ws is None:
            raise CodexBridgeError("Client is not connected")
        _check_cancelled(self.cancel_event)
        eff_timeout = self.rpc_timeout if timeout is None else max(0.05, float(timeout))
        deadline = time.monotonic() + eff_timeout

        self._next_id += 1
        req_id = f"agy-{self._next_id}"
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        rem = deadline - time.monotonic()
        if rem <= 0:
            raise TimeoutError(f"Timed out before sending {method}")
        self.ws.send_json(payload, timeout=rem, cancel_event=self.cancel_event)

        while True:
            _check_cancelled(self.cancel_event)
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError(f"Timed out waiting for response to {method}")
            raw = self.ws.recv_text(timeout=rem, cancel_event=self.cancel_event)
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise CodexBridgeError(
                    f"Codex app-server sent invalid JSON: {exc}"
                ) from exc
            if not isinstance(msg, dict):
                continue

            # Only match JSON-RPC responses (never server-initiated requests, which carry `method`).
            if "method" not in msg and msg.get("id") == req_id:
                if "error" in msg and msg["error"] is not None:
                    raise CodexRpcError(msg["error"])
                return msg.get("result")

            if "method" in msg and "id" in msg:
                self._handle_server_request(msg, max(0.0, deadline - time.monotonic()))
            elif "method" in msg and "id" not in msg:
                self.notifications.append(msg)
