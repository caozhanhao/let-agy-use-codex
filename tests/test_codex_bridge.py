from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import codex_app_client as client_mod  # noqa: E402
import codex_bridge as bridge  # noqa: E402
import mcp_hot_reload as hot_reload  # noqa: E402


class FakeTransport:
    def __init__(self, incoming: bytes = b""):
        self.incoming = bytearray(incoming)
        self.written = bytearray()
        self.closed = False

    def read_chunk(self, timeout: float) -> bytes:
        if not self.incoming:
            raise client_mod.CodexBridgeError("EOF")
        out = bytes(self.incoming)
        self.incoming.clear()
        return out

    def write_all(self, data: bytes) -> None:
        self.written.extend(data)

    def close(self) -> None:
        self.closed = True


def _build_server_ws_frame(payload: bytes, opcode: int = 0x1, fin: bool = True) -> bytes:
    b1 = (0x80 if fin else 0x00) | opcode
    n = len(payload)
    if n < 126:
        return bytes([b1, n]) + payload
    if n < 65536:
        return bytes([b1, 126]) + n.to_bytes(2, "big") + payload
    return bytes([b1, 127]) + n.to_bytes(8, "big") + payload


class WebSocketClientTests(unittest.TestCase):
    def test_handshake_and_fragmented_frames_with_ping(self) -> None:
        http_resp = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n"
        ping = _build_server_ws_frame(b"hi", opcode=0x9, fin=True)
        part1 = _build_server_ws_frame(b'{"jsonrpc":"2.0",', opcode=0x1, fin=False)
        part2 = _build_server_ws_frame(b'"id":1,"result":{"ok":true}}', opcode=0x0, fin=True)
        transport = FakeTransport(http_resp + ping + part1 + part2)

        ws = client_mod.WebSocketClient(transport)
        status = ws.handshake()
        self.assertIn("101", status)

        msg = json.loads(ws.recv_text(timeout=2.0))
        self.assertEqual(msg["result"], {"ok": True})
        # Verify pong frame (opcode 0xA, FIN=1 -> 0x8A) was sent back
        self.assertIn(b"\x8a", bytes(transport.written))


class FormattingAndGuardrailTests(unittest.TestCase):
    def test_format_turn_drops_empty_reasoning_and_compacts_outputs(self) -> None:
        turn = {
            "id": "t1",
            "status": "completed",
            "items": [
                {"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": "hello"}]},
                {"type": "reasoning", "id": "r1", "summary": []},
                {"type": "reasoning", "id": "r2", "summary": ["checked foo.py"]},
                {
                    "type": "commandExecution",
                    "id": "c1",
                    "command": "pytest",
                    "status": "completed",
                    "exitCode": 0,
                    "aggregatedOutput": "A" * 5000,
                },
                {
                    "type": "fileChange",
                    "id": "f1",
                    "status": "completed",
                    "changes": [{"path": "foo.py", "kind": "update", "diff": "+x\n" * 500}],
                },
                {"type": "agentMessage", "id": "a1", "text": "done"},
            ],
        }
        compact = bridge.format_turn(turn, include_tool_outputs=False)
        self.assertEqual(compact["omittedEmptyReasoningCount"], 1)
        types = [it["type"] for it in compact["items"]]
        self.assertEqual(types, ["userMessage", "reasoning", "commandExecution", "fileChange", "agentMessage"])
        self.assertNotIn("output", compact["items"][2])
        self.assertEqual(compact["items"][2]["outputChars"], 5000)
        self.assertNotIn("diff", compact["items"][3]["changes"][0])

        full = bridge.format_turn(turn, include_tool_outputs=True, max_output_chars=100)
        self.assertTrue(full["items"][2]["truncated"])
        self.assertTrue(full["items"][3]["changes"][0]["truncated"])

    def test_modify_guardrail_blocks_without_confirmation(self) -> None:
        for fn, args in [
            (bridge.op_send_turn, {"thread_id": "t", "prompt": "hi", "confirm_modify_thread": False}),
            (bridge.op_steer_turn, {"thread_id": "t", "prompt": "hi"}),
            (bridge.op_queue_message, {"thread_id": "t", "message": "hi"}),
            (bridge.op_interrupt_turn, {"thread_id": "t"}),
        ]:
            with self.assertRaises(client_mod.CodexBridgeError) as ctx:
                fn(args, {})
            self.assertIn("confirm_modify_thread=true", str(ctx.exception))

    def test_send_turn_refuses_when_in_progress(self) -> None:
        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.call.side_effect = [
            {"thread": {"id": "tid", "status": {"type": "active", "activeFlags": []}}},
            {"data": [{"id": "turn-1", "status": "inProgress"}]},
        ]
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_client):
            with self.assertRaises(client_mod.CodexBridgeError) as ctx:
                bridge.op_send_turn(
                    {"thread_id": "tid", "prompt": "hi", "confirm_modify_thread": True},
                    {},
                )
            self.assertIn("inProgress", str(ctx.exception))

    def test_wait_turn_returns_immediately_on_user_approval_flag(self) -> None:
        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.call.side_effect = [
            {"thread": {"id": "tid", "status": {"type": "active", "activeFlags": ["waitingOnApproval"]}}},
            {"data": [{"id": "turn-9", "status": "inProgress", "items": []}]},
            # Full hydration on return
            {"data": [{"id": "turn-9", "status": "inProgress", "items": []}]},
        ]
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_client):
            res = bridge.op_wait_turn({"thread_id": "tid", "timeout_seconds": 5}, {})
            self.assertEqual(res["waitOutcome"], "waiting_for_user")
            self.assertEqual(res["activeFlags"], ["waitingOnApproval"])

    def test_wait_turn_surfaces_mid_turn_steers_and_follows_follow_up_turn(self) -> None:
        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.call.side_effect = [
            # Poll 1: T1 completed, but T2 (user follow-up) is inProgress & thread is active
            {"thread": {"id": "tid", "status": {"type": "active", "activeFlags": []}}},
            {
                "data": [
                    {"id": "turn-2", "status": "inProgress", "items": []},
                    {"id": "turn-1", "status": "completed", "items": []},
                ]
            },
            # Poll 2: both done and thread idle
            {"thread": {"id": "tid", "status": {"type": "idle"}}},
            {
                "data": [
                    {"id": "turn-2", "status": "completed", "items": []},
                    {"id": "turn-1", "status": "completed", "items": []},
                ]
            },
            # Final hydration with itemsView="full"
            {
                "data": [
                    {
                        "id": "turn-2",
                        "status": "completed",
                        "items": [
                            {"type": "userMessage", "id": "u3", "content": [{"type": "text", "text": "also add a test"}]},
                            {"type": "agentMessage", "id": "a2", "text": "added test"},
                        ],
                    },
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "items": [
                            {"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": "initial task"}]},
                            {"type": "userMessage", "id": "u2", "content": [{"type": "text", "text": "actually use option B"}]},
                            {"type": "agentMessage", "id": "a1", "text": "switched to option B"},
                        ],
                    },
                ]
            },
        ]
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_client), mock.patch.object(
            bridge.time, "sleep"
        ):
            res = bridge.op_wait_turn(
                {"thread_id": "tid", "turn_id": "turn-1", "timeout_seconds": 10}, {}
            )
            self.assertEqual(res["waitOutcome"], "completed")
            self.assertEqual(res["watchedTurnIds"], ["turn-1", "turn-2"])
            self.assertEqual(len(res["turns"]), 2)
            self.assertEqual(res["turns"][0]["midTurnSteers"][0]["text"], "actually use option B")
            self.assertEqual(
                [(x["kind"], x["text"]) for x in res["userInterventions"]],
                [
                    ("mid_turn_steer", "actually use option B"),
                    ("follow_up_turn", "also add a test"),
                ],
            )

    def test_wait_turn_can_return_early_on_user_steer_when_enabled(self) -> None:
        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.call.side_effect = [
            # Poll 1: baseline 1 userMessage
            {"thread": {"id": "tid", "status": {"type": "active", "activeFlags": []}}},
            {
                "data": [
                    {
                        "id": "turn-1",
                        "status": "inProgress",
                        "items": [{"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": "go"}]}],
                    }
                ]
            },
            # Poll 2: user steered mid-turn -> 2 userMessages
            {"thread": {"id": "tid", "status": {"type": "active", "activeFlags": []}}},
            {
                "data": [
                    {
                        "id": "turn-1",
                        "status": "inProgress",
                        "items": [
                            {"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": "go"}]},
                            {"type": "userMessage", "id": "u2", "content": [{"type": "text", "text": "change direction"}]},
                        ],
                    }
                ]
            },
            # Final hydration
            {
                "data": [
                    {
                        "id": "turn-1",
                        "status": "inProgress",
                        "items": [
                            {"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": "go"}]},
                            {"type": "userMessage", "id": "u2", "content": [{"type": "text", "text": "change direction"}]},
                        ],
                    }
                ]
            },
        ]
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_client), mock.patch.object(
            bridge.time, "sleep"
        ):
            res = bridge.op_wait_turn(
                {
                    "thread_id": "tid",
                    "turn_id": "turn-1",
                    "return_on_user_steer": True,
                    "timeout_seconds": 10,
                },
                {},
            )
            self.assertEqual(res["waitOutcome"], "user_steered")
            self.assertEqual(res["userInterventions"][0]["text"], "change direction")

    def test_start_thread_inherits_codex_config_defaults_when_omitted(self) -> None:
        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.call.return_value = {
            "thread": {"id": "tid-new"},
            "cwd": REPO_ROOT,
            "model": "codex-test-model",
            "reasoningEffort": "xhigh",
            "sandbox": {"type": "workspaceWrite"},
            "approvalPolicy": "on-request",
            "approvalsReviewer": "auto_review",
        }
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_client):
            res = bridge.op_start_thread({"cwd": REPO_ROOT}, {})
            self.assertEqual(res["threadId"], "tid-new")
            self.assertEqual(res["reasoningEffort"], "xhigh")
            method, params = fake_client.call.call_args[0]
            self.assertEqual(method, "thread/start")
            self.assertEqual(params, {"cwd": REPO_ROOT})

    def test_start_thread_and_review_propagate_effort_config(self) -> None:
        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.call.return_value = {
            "thread": {"id": "tid-eff"},
            "cwd": REPO_ROOT,
            "model": "codex-test-model",
            "reasoningEffort": "high",
        }
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_client):
            res = bridge.op_start_thread(
                {"cwd": REPO_ROOT, "model": "codex-test-model", "effort": "high"}, {}
            )
            self.assertEqual(res["reasoningEffort"], "high")
            method, params = fake_client.call.call_args[0]
            self.assertEqual(method, "thread/start")
            self.assertEqual(
                params,
                {
                    "cwd": REPO_ROOT,
                    "model": "codex-test-model",
                    "config": {"model_reasoning_effort": "high"},
                },
            )

        fake_client2 = mock.MagicMock()
        fake_client2.__enter__.return_value = fake_client2
        fake_client2.call.side_effect = [
            {
                "thread": {"id": "tid-rev"},
                "model": "codex-test-model",
                "reasoningEffort": "xhigh",
            },
            {},
            {"turn": {"id": "rev-1", "status": "inProgress"}},
        ]
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_client2):
            res2 = bridge.op_start_review(
                {"cwd": REPO_ROOT, "model": "codex-test-model", "effort": "xhigh"}, {}
            )
            self.assertEqual(res2["model"], "codex-test-model")
            self.assertEqual(res2["reasoningEffort"], "xhigh")
            first_method, first_params = fake_client2.call.call_args_list[0][0]
            self.assertEqual(first_method, "thread/start")
            self.assertEqual(
                first_params,
                {
                    "cwd": REPO_ROOT,
                    "sandbox": "read-only",
                    "model": "codex-test-model",
                    "config": {"model_reasoning_effort": "xhigh"},
                },
            )


class McpProtocolTests(unittest.TestCase):
    def test_mcp_initialize_and_tools_list(self) -> None:
        server = bridge.McpStdioServer({})
        init_resp = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test"}},
            }
        )
        assert init_resp is not None
        self.assertEqual(init_resp["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(init_resp["result"]["serverInfo"]["name"], "let-agy-use-codex")

        notif_resp = server.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertIsNone(notif_resp)

        list_resp = server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert list_resp is not None
        names = [t["name"] for t in list_resp["result"]["tools"]]
        self.assertIn("codex_read_thread", names)
        self.assertIn("codex_start_thread", names)
        self.assertIn("codex_wait_turn", names)
        self.assertEqual(len(names), 14)

    def test_mcp_jsonl_and_content_length_framing(self) -> None:
        req = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}).encode("utf-8")

        # JSONL mode
        in_buf = io.BytesIO(req + b"\n")
        out_buf = io.BytesIO()
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=in_buf)), mock.patch.object(
            sys, "stdout", mock.Mock(buffer=out_buf)
        ):
            bridge.McpStdioServer({}).serve_forever()
        out_msg = json.loads(out_buf.getvalue().decode("utf-8"))
        self.assertEqual(out_msg, {"jsonrpc": "2.0", "id": 7, "result": {}})

        # Content-Length mode
        framed = f"Content-Length: {len(req)}\r\n\r\n".encode("ascii") + req
        in_buf2 = io.BytesIO(framed)
        out_buf2 = io.BytesIO()
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=in_buf2)), mock.patch.object(
            sys, "stdout", mock.Mock(buffer=out_buf2)
        ):
            bridge.McpStdioServer({}).serve_forever()
        raw_out = out_buf2.getvalue()
        self.assertTrue(raw_out.startswith(b"Content-Length:"))
        body = raw_out.split(b"\r\n\r\n", 1)[1]
        self.assertEqual(json.loads(body.decode("utf-8")), {"jsonrpc": "2.0", "id": 7, "result": {}})


class HotReloadTests(unittest.TestCase):
    def test_hot_reload_picks_up_edits_and_survives_syntax_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client_name = "_tmp_hr_client"
            bridge_name = "_tmp_hr_bridge"
            client_path = os.path.join(tmp, f"{client_name}.py")
            bridge_path = os.path.join(tmp, f"{bridge_name}.py")

            def write_pair(val: str, extra_tool: bool = False, broken: bool = False) -> None:
                with open(client_path, "w", encoding="utf-8") as f:
                    f.write(f"VALUE = {val!r}\n")
                with open(bridge_path, "w", encoding="utf-8") as f:
                    if broken:
                        f.write("def broken(:\n")
                    else:
                        tools_expr = '[{"name": "t1"}]' if not extra_tool else '[{"name": "t1"}, {"name": "t2"}]'
                        f.write(
                            f"from {client_name} import VALUE\n"
                            "class McpStdioServer:\n"
                            "    def __init__(self, conn_kw):\n"
                            "        self.framing = 'jsonl'\n"
                            "    def handle_request(self, req):\n"
                            "        m = req.get('method')\n"
                            "        if m == 'initialize':\n"
                            "            return {'jsonrpc':'2.0','id':req['id'],'result':{'capabilities':{'tools':{}}}}\n"
                            "        if m == 'tools/list':\n"
                            f"            return {{'jsonrpc':'2.0','id':req['id'],'result':{{'tools':{tools_expr}}}}}\n"
                            "        if m == 'tools/call':\n"
                            "            return {'jsonrpc':'2.0','id':req['id'],'result':{'value': VALUE}}\n"
                            "        return None\n"
                        )
                # Ensure distinct mtime_ns even on coarse filesystems
                future = time.time_ns() + (5_000_000_000 if val == "v2" else (10_000_000_000 if broken else 0))
                os.utime(client_path, ns=(future, future))
                os.utime(bridge_path, ns=(future, future))

            write_pair("v1")
            sys.path.insert(0, tmp)
            try:
                srv = hot_reload.HotReloadMcpServer(
                    {}, client_module_name=client_name, bridge_module_name=bridge_name
                )
                init_msgs = srv.process_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
                self.assertTrue(init_msgs[0]["result"]["capabilities"]["tools"]["listChanged"])

                r1 = srv.process_request({"jsonrpc": "2.0", "id": 2, "method": "tools/call"})
                self.assertEqual(len(r1), 1)
                self.assertEqual(r1[0]["result"]["value"], "v1")

                # Update both modules and add a tool -> reloads & emits list_changed
                write_pair("v2", extra_tool=True)
                r2 = srv.process_request({"jsonrpc": "2.0", "id": 3, "method": "tools/call"})
                self.assertEqual(r2[0]["result"]["value"], "v2")
                self.assertEqual(len(r2), 2)
                self.assertEqual(r2[1]["method"], "notifications/tools/list_changed")

                # Introduce syntax error -> keeps v2 alive without crashing
                write_pair("v3", broken=True)
                r3 = srv.process_request({"jsonrpc": "2.0", "id": 4, "method": "tools/call"})
                self.assertEqual(r3[0]["result"]["value"], "v2")
            finally:
                sys.path.remove(tmp)
                sys.modules.pop(client_name, None)
                sys.modules.pop(bridge_name, None)


if __name__ == "__main__":
    unittest.main()
