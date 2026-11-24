#!/usr/bin/env python3
"""Read-only end-to-end smoke test against the live local Codex app-server daemon."""
from __future__ import annotations

import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCP_ENTRY = os.path.join(REPO_ROOT, "scripts", "mcp_hot_reload.py")


def main() -> int:
    proc = subprocess.Popen(
        [sys.executable, MCP_ENTRY],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

    def send(msg: dict) -> None:
        raw = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        proc.stdin.write(raw)
        proc.stdin.flush()

    def recv() -> dict:
        line = proc.stdout.readline()
        if not line:
            err = proc.stderr.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"MCP server exited early: {err}")
        return json.loads(line.decode("utf-8"))

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "smoke-test", "version": "0.1.0"},
                },
            }
        )
        init_resp = recv()
        assert init_resp["result"]["serverInfo"]["name"] == "let-agy-use-codex"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools_resp = recv()
        tool_names = [t["name"] for t in tools_resp["result"]["tools"]]
        print(f"MCP tools/list OK ({len(tool_names)} tools): {', '.join(tool_names)}")

        send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "codex_status", "arguments": {}},
            }
        )
        status_resp = recv()
        assert not status_resp["result"]["isError"], status_resp
        status_data = json.loads(status_resp["result"]["content"][0]["text"])
        print("MCP codex_status OK:", json.dumps(status_data, ensure_ascii=False))

        send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "codex_list_threads", "arguments": {"limit": 3}},
            }
        )
        list_resp = recv()
        assert not list_resp["result"]["isError"], list_resp
        list_data = json.loads(list_resp["result"]["content"][0]["text"])
        print(
            f"MCP codex_list_threads OK: loadedCount={list_data['loadedCount']}, "
            f"returned={len(list_data['threads'])}"
        )

        if list_data["threads"]:
            first_id = list_data["threads"][0]["id"]
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "codex_read_thread",
                        "arguments": {
                            "thread_id": first_id,
                            "recent_turns": 1,
                            "items_view": "compact",
                        },
                    },
                }
            )
            read_resp = recv()
            assert not read_resp["result"]["isError"], read_resp
            read_data = json.loads(read_resp["result"]["content"][0]["text"])
            recent = read_data.get("recentTurns") or []
            print(
                f"MCP codex_read_thread OK: id={read_data['thread']['id']}, "
                f"name={read_data['thread'].get('name')!r}, "
                f"status={read_data['thread'].get('status')}, "
                f"recentTurns={len(recent)}"
            )
            if recent and recent[0].get("id"):
                turn_id = recent[0]["id"]
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": 6,
                        "method": "tools/call",
                        "params": {
                            "name": "codex_get_turn_details",
                            "arguments": {
                                "thread_id": first_id,
                                "turn_id": turn_id,
                                "max_output_chars": 1000,
                            },
                        },
                    }
                )
                det_resp = recv()
                assert not det_resp["result"]["isError"], det_resp
                det_data = json.loads(det_resp["result"]["content"][0]["text"])
                item_types = {it.get("type") for it in (det_data.get("items") or [])}
                assert "unknown" not in item_types, f"Unwrapped ThreadItemEntry failed: {item_types}"
                print(
                    f"MCP codex_get_turn_details OK: turnId={turn_id}, "
                    f"count={det_data['count']}, types={sorted(t for t in item_types if t)}"
                )
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
