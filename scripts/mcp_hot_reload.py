#!/usr/bin/env python3
"""Persistent MCP stdio shell with automatic mtime-based hot reload.

Keeps the Antigravity <-> MCP stdio pipe alive while automatically reloading
`codex_app_client.py` and `codex_bridge.py` whenever either file changes on
disk. Idle overhead is zero; steady-state per-call overhead is two `os.stat`
calls (~50 microseconds).
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


def _file_sig(path: str) -> Tuple[int, int]:
    st = os.stat(path)
    return (st.st_mtime_ns, st.st_size)


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

        self.client_mod = importlib.import_module(client_module_name)
        self.bridge_mod = importlib.import_module(bridge_module_name)
        self._client_path = os.path.abspath(self.client_mod.__file__ or "")
        self._bridge_path = os.path.abspath(self.bridge_mod.__file__ or "")
        self._client_sig = _file_sig(self._client_path)
        self._bridge_sig = _file_sig(self._bridge_path)
        self._last_failed_sigs: Optional[Tuple[Tuple[int, int], Tuple[int, int]]] = None
        self._server = self.bridge_mod.McpStdioServer(conn_kw)
        self._tools_fingerprint = self._compute_tools_fingerprint()

    def _compute_tools_fingerprint(self) -> str:
        resp = self._server.handle_request({"jsonrpc": "2.0", "id": 0, "method": "tools/list"})
        tools = ((resp or {}).get("result") or {}).get("tools") or []
        return json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def maybe_reload(self) -> Tuple[bool, bool]:
        """Reload modules if disk files changed.

        Returns `(reloaded, tools_changed)`. Pre-compiles modified files before
        reloading so partial edits / syntax errors never disturb the active
        module state.
        """
        try:
            new_client_sig = _file_sig(self._client_path)
            new_bridge_sig = _file_sig(self._bridge_path)
        except OSError:
            return False, False

        client_changed = new_client_sig != self._client_sig
        bridge_changed = new_bridge_sig != self._bridge_sig
        if not client_changed and not bridge_changed:
            return False, False
        if self._last_failed_sigs == (new_client_sig, new_bridge_sig):
            return False, False

        prev_client_mod = self.client_mod
        prev_bridge_mod = self.bridge_mod
        prev_server = self._server
        try:
            for changed, path in (
                (client_changed, self._client_path),
                (bridge_changed, self._bridge_path),
            ):
                if changed:
                    with open(path, "r", encoding="utf-8") as f:
                        compile(f.read(), path, "exec")

            importlib.invalidate_caches()
            if client_changed:
                self.client_mod = importlib.reload(self.client_mod)
            # Always reload bridge when client changes so `from ... import` bindings refresh.
            self.bridge_mod = importlib.reload(self.bridge_mod)
            self._server = self.bridge_mod.McpStdioServer(self.conn_kw)
            self._server.framing = self.framing
            self._client_sig = new_client_sig
            self._bridge_sig = new_bridge_sig
            self._last_failed_sigs = None

            new_fp = self._compute_tools_fingerprint()
            tools_changed = new_fp != self._tools_fingerprint
            self._tools_fingerprint = new_fp
            print(
                f"[let-agy-use-codex] Hot-reloaded MCP modules (tools_changed={tools_changed})",
                file=sys.stderr,
            )
            return True, tools_changed
        except Exception as exc:
            self.client_mod = prev_client_mod
            self.bridge_mod = prev_bridge_mod
            self._server = prev_server
            self._last_failed_sigs = (new_client_sig, new_bridge_sig)
            print(
                f"[let-agy-use-codex] Hot-reload failed, keeping previous code: {exc}",
                file=sys.stderr,
            )
            return False, False

    def _read_message(self) -> Optional[Dict[str, Any]]:
        stdin = sys.stdin.buffer
        while True:
            line = stdin.readline()
            if not line:
                return None
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.lower().startswith(b"content-length:"):
                self.framing = "content-length"
                length = int(stripped.split(b":", 1)[1].strip())
                while True:
                    hdr = stdin.readline()
                    if not hdr or hdr in (b"\r\n", b"\n"):
                        break
                body = stdin.read(length)
                if not body:
                    return None
                return json.loads(body.decode("utf-8"))
            self.framing = "jsonl"
            return json.loads(stripped.decode("utf-8"))

    def _write_message(self, payload: Dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        stdout = sys.stdout.buffer
        if self.framing == "content-length":
            stdout.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii"))
            stdout.write(raw)
        else:
            stdout.write(raw + b"\n")
        stdout.flush()

    def process_request(self, req: Dict[str, Any]) -> List[Dict[str, Any]]:
        method = req.get("method")
        _, tools_changed = self.maybe_reload()
        self._server.framing = self.framing
        resp = self._server.handle_request(req)

        if method == "initialize" and resp and isinstance(resp.get("result"), dict):
            caps = resp["result"].setdefault("capabilities", {})
            tools_cap = caps.setdefault("tools", {})
            if isinstance(tools_cap, dict):
                tools_cap["listChanged"] = True
            self.initialized = True
        elif method == "notifications/initialized":
            self.initialized = True

        outgoing: List[Dict[str, Any]] = []
        if resp is not None:
            outgoing.append(resp)
        if tools_changed and self.initialized and method != "tools/list":
            outgoing.append(
                {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
            )
        return outgoing

    def serve_forever(self) -> None:
        while True:
            try:
                req = self._read_message()
            except Exception as exc:
                print(f"[let-agy-use-codex] MCP read error: {exc}", file=sys.stderr)
                continue
            if req is None:
                break
            for msg in self.process_request(req):
                self._write_message(msg)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp_hot_reload.py",
        description="Hot-reloading MCP stdio wrapper for let-agy-use-codex.",
    )
    parser.add_argument("--sock", help="Custom Codex app-server Unix control socket path.")
    parser.add_argument("--remote", help="Remote Codex app-server URL (ws://HOST:PORT or unix://PATH).")
    parser.add_argument("--auth-token", help="Optional bearer token for ws:// remote server.")
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
