#!/usr/bin/env python3
"""Persistent MCP stdio shell with atomic isolated hot reload & cancellation.

Keeps the Antigravity <-> MCP stdio pipe alive while atomically reloading
`codex_app_client.py` and `codex_bridge.py` whenever either file changes on
disk. Reloads execute fresh source bytes in isolated module namespaces before
swapping, so syntax errors or mid-import runtime exceptions never leave behind
partially mutated module state.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sys
import threading
import types
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


def _file_sig(path: str) -> Tuple[int, int, str]:
    st = os.stat(path)
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    return (st.st_mtime_ns, st.st_size, digest)


def _fast_stat_sig(path: str) -> Tuple[int, int]:
    st = os.stat(path)
    return (st.st_mtime_ns, st.st_size)


def _valid_request_id(req_id: Any) -> bool:
    return (
        isinstance(req_id, str)
        or (isinstance(req_id, int) and not isinstance(req_id, bool))
    )


def _jsonrpc_error(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def _load_isolated_module(name: str, path: str, source_bytes: bytes) -> types.ModuleType:
    source_text = source_bytes.decode("utf-8")
    code = compile(source_text, path, "exec")
    mod = types.ModuleType(name)
    mod.__file__ = path
    mod.__package__ = ""
    exec(code, mod.__dict__)
    return mod


class HotReloadMcpServer:
    def __init__(
        self,
        conn_kw: Dict[str, Any],
        *,
        client_module_name: str = "codex_app_client",
        bridge_module_name: str = "codex_bridge",
    ):
        self.conn_kw = conn_kw
        self.client_module_name = client_module_name
        self.bridge_module_name = bridge_module_name
        self.framing = "jsonl"
        self.initialized = False
        self.negotiated_protocol: Optional[str] = None

        self._reload_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._active_lock = threading.Lock()
        self._active_requests: Dict[Any, threading.Event] = {}
        self._workers: List[threading.Thread] = []

        self.client_mod = importlib.import_module(client_module_name)
        self.bridge_mod = importlib.import_module(bridge_module_name)
        self._client_path = os.path.abspath(self.client_mod.__file__ or "")
        self._bridge_path = os.path.abspath(self.bridge_mod.__file__ or "")
        self._client_stat = _fast_stat_sig(self._client_path)
        self._bridge_stat = _fast_stat_sig(self._bridge_path)
        self._client_sig = _file_sig(self._client_path)
        self._bridge_sig = _file_sig(self._bridge_path)
        self._last_failed_sigs: Optional[Tuple[Tuple[int, int, str], Tuple[int, int, str]]] = None
        self._server = self.bridge_mod.McpStdioServer(conn_kw)
        self._tools_fingerprint = self._compute_tools_fingerprint(self._server)

    @staticmethod
    def _compute_tools_fingerprint(server: Any) -> str:
        resp = server.handle_request({"jsonrpc": "2.0", "id": 0, "method": "tools/list"})
        tools = ((resp or {}).get("result") or {}).get("tools") or []
        return json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def maybe_reload(self) -> Tuple[bool, bool]:
        """Atomically reload modules in isolated namespaces if disk files changed.

        Returns `(reloaded, tools_changed)`. Builds BOTH new modules, constructs
        a fresh `McpStdioServer`, and computes the tool fingerprint before
        swapping anything, so any syntax or runtime error leaves active state
        completely untouched (and avoids `.pyc` same-second timestamp staleness).
        """
        with self._reload_lock:
            try:
                new_client_stat = _fast_stat_sig(self._client_path)
                new_bridge_stat = _fast_stat_sig(self._bridge_path)
            except OSError:
                return False, False

            if new_client_stat == self._client_stat and new_bridge_stat == self._bridge_stat:
                return False, False

            try:
                with open(self._client_path, "rb") as f:
                    client_bytes = f.read()
                with open(self._bridge_path, "rb") as f:
                    bridge_bytes = f.read()
                new_client_sig = (
                    new_client_stat[0],
                    new_client_stat[1],
                    hashlib.sha256(client_bytes).hexdigest(),
                )
                new_bridge_sig = (
                    new_bridge_stat[0],
                    new_bridge_stat[1],
                    hashlib.sha256(bridge_bytes).hexdigest(),
                )
            except OSError:
                return False, False

            if (
                new_client_sig[2] == self._client_sig[2]
                and new_bridge_sig[2] == self._bridge_sig[2]
            ):
                self._client_stat = new_client_stat
                self._bridge_stat = new_bridge_stat
                return False, False

            if self._last_failed_sigs == (new_client_sig, new_bridge_sig):
                return False, False

            prev_sys_client = sys.modules.get(self.client_module_name)
            prev_sys_bridge = sys.modules.get(self.bridge_module_name)
            try:
                # 1. Pre-compile both files before executing either.
                compile(client_bytes.decode("utf-8"), self._client_path, "exec")
                compile(bridge_bytes.decode("utf-8"), self._bridge_path, "exec")

                # 2. Build fresh isolated client module.
                new_client_mod = _load_isolated_module(
                    self.client_module_name, self._client_path, client_bytes
                )

                # 3. Build fresh isolated bridge module bound to new_client_mod.
                sys.modules[self.client_module_name] = new_client_mod
                try:
                    new_bridge_mod = _load_isolated_module(
                        self.bridge_module_name, self._bridge_path, bridge_bytes
                    )
                except Exception:
                    if prev_sys_client is not None:
                        sys.modules[self.client_module_name] = prev_sys_client
                    else:
                        sys.modules.pop(self.client_module_name, None)
                    raise

                # 4. Instantiate server & verify tools/list before committing swap.
                new_server = new_bridge_mod.McpStdioServer(self.conn_kw)
                new_server.framing = self.framing
                if self.negotiated_protocol is not None:
                    new_server.negotiated_protocol = self.negotiated_protocol
                new_fp = self._compute_tools_fingerprint(new_server)
            except Exception as exc:
                if prev_sys_client is not None:
                    sys.modules[self.client_module_name] = prev_sys_client
                else:
                    sys.modules.pop(self.client_module_name, None)
                if prev_sys_bridge is not None:
                    sys.modules[self.bridge_module_name] = prev_sys_bridge
                else:
                    sys.modules.pop(self.bridge_module_name, None)
                self._last_failed_sigs = (new_client_sig, new_bridge_sig)
                print(
                    f"[let-agy-use-codex] Hot-reload failed, keeping previous code: {exc}",
                    file=sys.stderr,
                )
                return False, False

            # 5. Atomic commit of both modules and server instance.
            sys.modules[self.client_module_name] = new_client_mod
            sys.modules[self.bridge_module_name] = new_bridge_mod
            self.client_mod = new_client_mod
            self.bridge_mod = new_bridge_mod
            self._server = new_server
            self._client_stat = new_client_stat
            self._bridge_stat = new_bridge_stat
            self._client_sig = new_client_sig
            self._bridge_sig = new_bridge_sig
            self._last_failed_sigs = None

            tools_changed = new_fp != self._tools_fingerprint
            self._tools_fingerprint = new_fp
            print(
                f"[let-agy-use-codex] Hot-reloaded MCP modules (tools_changed={tools_changed})",
                file=sys.stderr,
            )
            return True, tools_changed

    def _read_message(self) -> Tuple[Optional[Any], Optional[str]]:
        stdin = sys.stdin.buffer
        while True:
            line = stdin.readline()
            if not line:
                return None, None
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.lower().startswith(b"content-length:"):
                self.framing = "content-length"
                try:
                    length = int(stripped.split(b":", 1)[1].strip())
                    if length < 0:
                        return None, "Negative Content-Length header"
                except ValueError as exc:
                    return None, f"Invalid Content-Length header: {exc}"
                while True:
                    hdr = stdin.readline()
                    if not hdr or hdr in (b"\r\n", b"\n"):
                        break
                body = stdin.read(length)
                if not body or len(body) < length:
                    return None, None
                try:
                    return json.loads(body.decode("utf-8")), None
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    return None, f"Parse error: {exc}"
            self.framing = "jsonl"
            try:
                return json.loads(stripped.decode("utf-8")), None
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return None, f"Parse error: {exc}"

    def _write_message(self, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        stdout = sys.stdout.buffer
        with self._write_lock:
            if self.framing == "content-length":
                stdout.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii"))
                stdout.write(raw)
            else:
                stdout.write(raw + b"\n")
            stdout.flush()

    def cancel_request(self, request_id: Any) -> bool:
        with self._active_lock:
            ev = self._active_requests.get(request_id)
            if ev is not None:
                ev.set()
                return True
        return False

    def _handle_single(
        self,
        server: Any,
        req: Any,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> Optional[Dict[str, Any]]:
        if isinstance(req, dict) and "id" not in req and req.get("method") == "notifications/cancelled":
            params = req.get("params")
            if isinstance(params, dict) and params.get("requestId") is not None:
                self.cancel_request(params.get("requestId"))
            return None

        try:
            resp = server.handle_request(req, cancel_event=cancel_event)
        except TypeError:
            resp = server.handle_request(req)

        if isinstance(req, dict):
            method = req.get("method")
            if method == "initialize" and resp and isinstance(resp.get("result"), dict):
                caps = resp["result"].setdefault("capabilities", {})
                tools_cap = caps.setdefault("tools", {})
                if isinstance(tools_cap, dict):
                    tools_cap["listChanged"] = True
                proto = resp["result"].get("protocolVersion")
                if isinstance(proto, str):
                    self.negotiated_protocol = proto
                self.initialized = True
            elif method == "notifications/initialized":
                self.initialized = True
        return resp

    def process_request(
        self,
        req: Any,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> List[Any]:
        _, tools_changed = self.maybe_reload()
        server = self._server
        server.framing = self.framing
        if self.negotiated_protocol is not None:
            server.negotiated_protocol = self.negotiated_protocol

        outgoing: List[Any] = []
        if isinstance(req, list):
            if not req:
                outgoing.append(
                    _jsonrpc_error(None, -32600, "Invalid Request: empty JSON-RPC batch")
                )
            elif self.negotiated_protocol == "2025-06-18":
                outgoing.append(
                    _jsonrpc_error(
                        None,
                        -32600,
                        "Invalid Request: JSON-RPC batching is not supported in MCP 2025-06-18",
                    )
                )
            else:
                batch_resps: List[Dict[str, Any]] = []
                for item in req:
                    r = self._handle_single(server, item, cancel_event=cancel_event)
                    if r is not None:
                        batch_resps.append(r)
                if batch_resps:
                    outgoing.append(batch_resps)
        else:
            resp = self._handle_single(server, req, cancel_event=cancel_event)
            if resp is not None:
                outgoing.append(resp)

        is_tools_list = isinstance(req, dict) and req.get("method") == "tools/list"
        if tools_changed and self.initialized and not is_tools_list:
            outgoing.append(
                {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
            )
        return outgoing

    def _dispatch_tool_call_async(self, req: Dict[str, Any]) -> None:
        _, tools_changed = self.maybe_reload()
        server = self._server
        server.framing = self.framing
        if self.negotiated_protocol is not None:
            server.negotiated_protocol = self.negotiated_protocol

        if tools_changed and self.initialized:
            self._write_message(
                {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
            )

        req_id = req["id"]
        cancel_event = threading.Event()
        with self._active_lock:
            self._active_requests[req_id] = cancel_event

        def _worker() -> None:
            try:
                resp = self._handle_single(server, req, cancel_event=cancel_event)
                if resp is not None and not cancel_event.is_set():
                    self._write_message(resp)
            finally:
                with self._active_lock:
                    self._active_requests.pop(req_id, None)

        thread = threading.Thread(
            target=_worker, name=f"mcp-hr-tool-{req_id}", daemon=True
        )
        self._workers = [t for t in self._workers if t.is_alive()]
        self._workers.append(thread)
        thread.start()

    def serve_forever(self) -> None:
        try:
            while True:
                msg, parse_err = self._read_message()
                if parse_err is not None:
                    self._write_message(_jsonrpc_error(None, -32700, parse_err))
                    continue
                if msg is None:
                    break
                if (
                    isinstance(msg, dict)
                    and msg.get("method") == "tools/call"
                    and "id" in msg
                    and _valid_request_id(msg.get("id"))
                ):
                    self._dispatch_tool_call_async(msg)
                    continue
                for out_msg in self.process_request(msg):
                    self._write_message(out_msg)
        finally:
            for t in list(self._workers):
                t.join(timeout=2.0)
            with self._active_lock:
                for ev in self._active_requests.values():
                    ev.set()
            for t in list(self._workers):
                if t.is_alive():
                    t.join(timeout=1.0)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp_hot_reload.py",
        description="Hot-reloading MCP stdio wrapper for let-agy-use-codex.",
    )
    parser.add_argument("--sock", help="Custom Codex app-server Unix control socket path.")
    parser.add_argument(
        "--remote",
        help="Remote Codex app-server URL (wss://HOST:PORT, ws://127.0.0.1:PORT, or unix://PATH).",
    )
    parser.add_argument(
        "--auth-token",
        help="Optional bearer token (allowed only over wss://, unix://, or loopback ws://).",
    )
    ns = parser.parse_args(argv)
    conn_kw = {
        "sock_path": ns.sock,
        "remote_url": ns.remote,
        "auth_token": ns.auth_token,
    }
    HotReloadMcpServer(conn_kw).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
