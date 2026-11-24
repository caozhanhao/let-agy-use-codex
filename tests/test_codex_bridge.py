from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
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
    """In-memory transport that computes valid RFC 6455 Sec-WebSocket-Accept automatically."""

    def __init__(
        self,
        incoming: bytes = b"",
        *,
        auto_handshake: bool = False,
        raw_handshake: bytes = b"",
    ):
        self.incoming = bytearray(incoming)
        self.written = bytearray()
        self.closed = False
        self._auto_handshake = auto_handshake
        self._raw_handshake = raw_handshake
        self._handshake_replied = False

    def read_chunk(self, timeout: float, cancel_event=None) -> bytes:
        if not self.incoming:
            raise client_mod.CodexBridgeError("EOF")
        out = bytes(self.incoming)
        self.incoming.clear()
        return out

    def write_all(self, data: bytes, timeout: float = 15.0, cancel_event=None) -> None:
        self.written.extend(data)
        if self._auto_handshake and not self._handshake_replied and b"Sec-WebSocket-Key:" in self.written:
            self._handshake_replied = True
            req_text = bytes(self.written).decode("utf-8", errors="replace")
            key = ""
            for line in req_text.split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
                    break
            accept = base64.b64encode(
                hashlib.sha1((key + client_mod.WS_GUID).encode("ascii")).digest()
            ).decode("ascii")
            resp = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
            self.incoming[:0] = resp
        elif self._raw_handshake and not self._handshake_replied:
            self._handshake_replied = True
            self.incoming[:0] = self._raw_handshake

    def close(self) -> None:
        self.closed = True


def _build_server_ws_frame(
    payload: bytes,
    opcode: int = 0x1,
    fin: bool = True,
    *,
    masked: bool = False,
    rsv: int = 0,
) -> bytes:
    b1 = (0x80 if fin else 0x00) | (rsv & 0x70) | (opcode & 0x0F)
    n = len(payload)
    mask_bit = 0x80 if masked else 0x00
    if n < 126:
        header = bytes([b1, mask_bit | n])
    elif n < 65536:
        header = bytes([b1, mask_bit | 126]) + n.to_bytes(2, "big")
    else:
        header = bytes([b1, mask_bit | 127]) + n.to_bytes(8, "big")
    if masked:
        mask = b"\x01\x02\x03\x04"
        body = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        return header + mask + body
    return header + payload


def _decode_client_ws_frames(raw: bytes) -> list:
    """Decode client-masked frames from `transport.written` after HTTP request."""
    idx = raw.find(b"\r\n\r\n")
    pos = idx + 4 if idx >= 0 else 0
    frames = []
    while pos + 2 <= len(raw):
        b1, b2 = raw[pos], raw[pos + 1]
        pos += 2
        opcode = b1 & 0x0F
        masked = (b2 & 0x80) != 0
        length = b2 & 0x7F
        if length == 126:
            length = int.from_bytes(raw[pos : pos + 2], "big")
            pos += 2
        elif length == 127:
            length = int.from_bytes(raw[pos : pos + 8], "big")
            pos += 8
        mask = raw[pos : pos + 4] if masked else None
        if masked:
            pos += 4
        payload = raw[pos : pos + length]
        pos += length
        if mask:
            payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        frames.append((opcode, payload))
    return frames


class WebSocketClientTests(unittest.TestCase):
    def test_handshake_and_fragmented_frames_with_ping(self) -> None:
        ping = _build_server_ws_frame(b"hi", opcode=0x9, fin=True)
        part1 = _build_server_ws_frame(b'{"jsonrpc":"2.0",', opcode=0x1, fin=False)
        part2 = _build_server_ws_frame(b'"id":1,"result":{"ok":true}}', opcode=0x0, fin=True)
        transport = FakeTransport(ping + part1 + part2, auto_handshake=True)

        ws = client_mod.WebSocketClient(transport)
        status = ws.handshake()
        self.assertIn("101", status)

        msg = json.loads(ws.recv_text(timeout=2.0))
        self.assertEqual(msg["result"], {"ok": True})
        frames = _decode_client_ws_frames(bytes(transport.written))
        self.assertTrue(any(op == 0xA and payload == b"hi" for op, payload in frames))

    def test_handshake_rejects_missing_accept_upgrade_connection_and_bad_status(self) -> None:
        bad_responses = [
            b"HTTP/1.1 200 OK\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n",
            b"HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\n\r\n",
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n",
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: wrong==\r\n\r\n",
        ]
        for raw in bad_responses:
            ws = client_mod.WebSocketClient(FakeTransport(raw_handshake=raw))
            with self.assertRaises(client_mod.CodexBridgeError):
                ws.handshake()

    def test_recv_rejects_masked_server_frame_bad_continuation_binary_and_bad_utf8(self) -> None:
        cases = [
            _build_server_ws_frame(b"hello", opcode=0x1, masked=True),
            _build_server_ws_frame(b"orphan", opcode=0x0, fin=True),
            _build_server_ws_frame(b"\x01\x02", opcode=0x2, fin=True),
            _build_server_ws_frame(b"\xff\xfe", opcode=0x1, fin=True),
            _build_server_ws_frame(b"x", opcode=0x1, fin=True, rsv=0x40),
        ]
        for frame in cases:
            ws = client_mod.WebSocketClient(FakeTransport(frame, auto_handshake=True))
            ws.handshake()
            with self.assertRaises(client_mod.CodexBridgeError):
                ws.recv_text(timeout=1.0)

    def test_socket_transport_handles_backpressure_without_blocking_io_error(self) -> None:
        s1, s2 = socket.socketpair()
        try:
            s1.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            s2.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            tr = client_mod._SocketTransport(s1, eof_context=lambda: "pair")
            payload = b"X" * 256_000
            received = bytearray()

            def reader() -> None:
                while len(received) < len(payload):
                    chunk = s2.recv(4096)
                    if not chunk:
                        break
                    received.extend(chunk)

            t = threading.Thread(target=reader, daemon=True)
            t.start()
            tr.write_all(payload, timeout=5.0)
            t.join(timeout=5.0)
            self.assertEqual(len(received), len(payload))
        finally:
            s1.close()
            s2.close()

    def test_remote_transport_blocks_non_loopback_ws_with_bearer_and_supports_wss(self) -> None:
        with self.assertRaises(client_mod.CodexBridgeError) as ctx:
            client_mod._open_remote_transport(
                "ws://remote.example.com:8080/ws", auth_token="secret-token"
            )
        self.assertIn("plaintext ws://", str(ctx.exception))

        # Loopback ws:// with bearer token is allowed
        fake_sock = mock.MagicMock()
        with mock.patch.object(
            client_mod.socket, "create_connection", return_value=fake_sock
        ):
            _, host, path, headers = client_mod._open_remote_transport(
                "ws://127.0.0.1:8080/rpc", auth_token="secret-token"
            )
            self.assertEqual(host, "127.0.0.1:8080")
            self.assertEqual(path, "/rpc")
            self.assertEqual(headers["Authorization"], "Bearer secret-token")

        # wss:// wraps TLS with server_hostname verification
        raw_sock = mock.MagicMock()
        tls_sock = mock.MagicMock()
        fake_ctx = mock.MagicMock()
        fake_ctx.wrap_socket.return_value = tls_sock
        with mock.patch.object(
            client_mod.socket, "create_connection", return_value=raw_sock
        ), mock.patch.object(
            client_mod.ssl, "create_default_context", return_value=fake_ctx
        ):
            _, host2, _, headers2 = client_mod._open_remote_transport(
                "wss://codex.example.com/ws", auth_token="secret-token"
            )
            self.assertEqual(host2, "codex.example.com")
            self.assertEqual(headers2["Authorization"], "Bearer secret-token")
            fake_ctx.wrap_socket.assert_called_once_with(
                raw_sock, server_hostname="codex.example.com"
            )

    def test_server_initiated_requests_rejected_or_deferred_and_id_isolated(self) -> None:
        # 1. Server sends an exclusive dynamic tool request with same id -> rejected with -32601
        # 2. Server sends a human approval request with same id -> deferred (NOT answered)
        # 3. Server sends actual response to our `agy-1` request
        dyn_req = _build_server_ws_frame(
            json.dumps(
                {"jsonrpc": "2.0", "id": "agy-1", "method": "item/tool/call", "params": {"tool": "foo"}}
            ).encode("utf-8")
        )
        approval_req = _build_server_ws_frame(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "srv-9",
                    "method": "item/commandExecution/requestApproval",
                    "params": {"itemId": "c1"},
                }
            ).encode("utf-8")
        )
        srv_resp = _build_server_ws_frame(
            json.dumps({"jsonrpc": "2.0", "id": "agy-1", "result": {"ok": 42}}).encode("utf-8")
        )
        transport = FakeTransport(dyn_req + approval_req + srv_resp, auto_handshake=True)
        ws = client_mod.WebSocketClient(transport)
        ws.handshake()
        client = client_mod.CodexAppClient()
        client.ws = ws
        res = client.call("ping", {})
        self.assertEqual(res, {"ok": 42})

        sent_msgs = [
            json.loads(payload.decode("utf-8"))
            for op, payload in _decode_client_ws_frames(bytes(transport.written))
            if op == 0x1
        ]
        # Must have sent our `ping` + a -32601 error for `item/tool/call`, and NO reply for approval `srv-9`
        error_replies = [m for m in sent_msgs if "error" in m]
        self.assertEqual(len(error_replies), 1)
        self.assertEqual(error_replies[0]["id"], "agy-1")
        self.assertEqual(error_replies[0]["error"]["code"], -32601)
        self.assertEqual(len(client.pending_server_requests), 2)
        self.assertEqual(client.pending_server_requests[0]["action"], "rejected_unsupported")
        self.assertEqual(client.pending_server_requests[1]["action"], "deferred_to_human_ui")


class FormattingAndGuardrailTests(unittest.TestCase):
    def test_format_turn_drops_empty_reasoning_compacts_outputs_and_enforces_budget(self) -> None:
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
                    "changes": [
                        {"path": "foo.py", "kind": "update", "diff": "+x\n" * 500},
                        {"path": "bar.py", "kind": "update", "diff": "+y\n" * 500},
                    ],
                },
                {"type": "agentMessage", "id": "a1", "phase": "final_answer", "text": "Z" * 20000},
            ],
        }
        compact = bridge.format_turn(turn, include_tool_outputs=False)
        self.assertEqual(compact["omittedEmptyReasoningCount"], 1)
        types = [it["type"] for it in compact["items"]]
        self.assertEqual(types, ["userMessage", "reasoning", "commandExecution", "fileChange", "agentMessage"])
        self.assertNotIn("output", compact["items"][2])
        self.assertEqual(compact["items"][2]["outputChars"], 5000)
        self.assertNotIn("diff", compact["items"][3]["changes"][0])
        # Long final_answer (20k) is NOT prematurely truncated at 8000 chars
        self.assertEqual(len(compact["items"][4]["text"]), 20000)
        self.assertNotIn("truncated", compact["items"][4])

        # Tight total budget caps combined tool outputs across items
        small_budget = bridge._OutputBudget(total_limit=1000)
        full = bridge.format_turn(
            turn, include_tool_outputs=True, max_output_chars=800, budget=small_budget
        )
        self.assertTrue(full["outputBudgetExhausted"])
        self.assertTrue(full["items"][2]["truncated"])
        self.assertTrue(full["items"][3]["changes"][1]["budgetTruncated"])

    def test_agent_message_moderation_display_withholds_hidden_and_blocked_text(self) -> None:
        hidden = bridge.format_item(
            {
                "type": "agentMessage",
                "id": "m1",
                "text": "sensitive raw text",
                "moderation": {
                    "moderated": True,
                    "display": {
                        "mode": "hide",
                        "version": 1,
                        "replacement": {"kind": "wellbeingPromptHelpline"},
                    },
                },
            }
        )
        assert hidden is not None
        self.assertNotIn("sensitive raw text", hidden["text"])
        self.assertTrue(hidden["textWithheld"])
        self.assertEqual(hidden["moderationDisplay"]["mode"], "hide")

        blocked = bridge.format_item(
            {
                "type": "agentMessage",
                "id": "m2",
                "text": "blocked raw text",
                "moderation": {
                    "moderated": True,
                    "display": {
                        "mode": "block",
                        "version": 1,
                        "reason": {"kind": "protection", "protectionType": "safety"},
                    },
                },
            }
        )
        assert blocked is not None
        self.assertNotIn("blocked raw text", blocked["text"])
        self.assertTrue(blocked["textWithheld"])

        unreadable = bridge.format_item(
            {
                "type": "agentMessage",
                "id": "m3",
                "text": "restricted raw text",
                "moderation": {"moderated": True, "display": None},
            }
        )
        assert unreadable is not None
        self.assertNotIn("restricted raw text", unreadable["text"])
        self.assertTrue(unreadable["textWithheld"])

        shown = bridge.format_item(
            {
                "type": "agentMessage",
                "id": "m4",
                "text": "normal answer",
                "moderation": {
                    "moderated": False,
                    "display": {"mode": "show", "version": 1, "trustedContacts": "skip"},
                },
            }
        )
        assert shown is not None
        self.assertEqual(shown["text"], "normal answer")
        self.assertNotIn("textWithheld", shown)

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

        # Existing-thread review requires BOTH confirm_modify_thread and confirm_replace_active_turn
        with self.assertRaises(client_mod.CodexBridgeError) as ctx2:
            bridge.op_start_review(
                {"thread_id": "t", "confirm_modify_thread": True}, {}
            )
        self.assertIn("confirm_replace_active_turn=true", str(ctx2.exception))

    def test_send_turn_refuses_when_in_progress_and_allows_empty_new_thread(self) -> None:
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

        # Empty brand-new thread where thread/turns/list returns "no rollout found"
        fake_empty = mock.MagicMock()
        fake_empty.__enter__.return_value = fake_empty
        fake_empty.call.side_effect = [
            {"thread": {"id": "tid-new", "status": {"type": "idle"}}},
            client_mod.CodexRpcError({"code": -32600, "message": "no rollout found for thread id tid-new"}),
            {"turn": {"id": "turn-first", "status": "inProgress"}},
        ]
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_empty):
            res = bridge.op_send_turn(
                {"thread_id": "tid-new", "prompt": "first", "confirm_modify_thread": True},
                {},
            )
            self.assertEqual(res["turnId"], "turn-first")

    def test_wait_turn_returns_immediately_on_user_approval_flag(self) -> None:
        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.call.side_effect = [
            {"thread": {"id": "tid", "status": {"type": "active", "activeFlags": ["waitingOnApproval"]}}},
            {"data": [{"id": "turn-9", "status": "inProgress", "items": []}]},
            {"data": [{"id": "turn-9", "status": "inProgress", "items": []}]},
        ]
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_client):
            res = bridge.op_wait_turn({"thread_id": "tid", "timeout_seconds": 5}, {})
            self.assertEqual(res["waitOutcome"], "waiting_for_user")
            self.assertEqual(res["activeFlags"], ["waitingOnApproval"])

    def test_wait_turn_no_follow_returns_only_anchor_even_when_b_is_active_with_flags(self) -> None:
        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value = fake_client
        turns_ab = {
            "data": [
                {"id": "turn-B", "status": "inProgress", "items": []},
                {"id": "turn-A", "status": "completed", "items": [{"type": "agentMessage", "id": "a1", "text": "done A"}]},
            ]
        }
        fake_client.call.side_effect = [
            {"thread": {"id": "tid", "status": {"type": "active", "activeFlags": ["waitingOnApproval"]}}},
            turns_ab,
            turns_ab,
        ]
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_client):
            res = bridge.op_wait_turn(
                {
                    "thread_id": "tid",
                    "turn_id": "turn-A",
                    "follow_new_turns": False,
                    "timeout_seconds": 5,
                },
                {},
            )
            self.assertEqual(res["waitOutcome"], "completed")
            self.assertEqual(res["watchedTurnIds"], ["turn-A"])
            self.assertEqual(res["turn"]["id"], "turn-A")
            self.assertEqual(res["turn"]["status"], "completed")

    def test_wait_turn_hydration_race_re_enters_loop_when_b_starts(self) -> None:
        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.call.side_effect = [
            # Poll 1: A completed & idle
            {"thread": {"id": "tid", "status": {"type": "idle"}}},
            {"data": [{"id": "turn-A", "status": "completed", "items": []}]},
            # Hydration sees turn-B started inProgress -> must NOT return completed+inProgress!
            {
                "data": [
                    {"id": "turn-B", "status": "inProgress", "items": []},
                    {"id": "turn-A", "status": "completed", "items": []},
                ]
            },
            # Poll 2: both A and B completed & idle
            {"thread": {"id": "tid", "status": {"type": "idle"}}},
            {
                "data": [
                    {"id": "turn-B", "status": "completed", "items": []},
                    {"id": "turn-A", "status": "completed", "items": []},
                ]
            },
            # Final hydration: both completed
            {
                "data": [
                    {"id": "turn-B", "status": "completed", "items": [{"type": "agentMessage", "id": "b1", "text": "done B"}]},
                    {"id": "turn-A", "status": "completed", "items": [{"type": "agentMessage", "id": "a1", "text": "done A"}]},
                ]
            },
        ]
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_client), mock.patch.object(
            bridge.time, "sleep"
        ):
            res = bridge.op_wait_turn(
                {"thread_id": "tid", "turn_id": "turn-A", "timeout_seconds": 10}, {}
            )
            self.assertEqual(res["waitOutcome"], "completed")
            self.assertEqual(res["turn"]["id"], "turn-B")
            self.assertEqual(res["turn"]["status"], "completed")

    def test_wait_turn_paginates_past_first_page_to_find_older_anchor(self) -> None:
        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value = fake_client
        page1 = [{"id": f"t-{i}", "status": "completed", "items": []} for i in range(25, 5, -1)]
        page2 = [{"id": f"t-{i}", "status": "completed", "items": []} for i in range(5, 0, -1)]

        def fake_call(method, params, **kwargs):
            if method == "thread/read":
                return {"thread": {"id": "tid", "status": {"type": "idle"}}}
            if method == "thread/turns/list":
                if not params.get("cursor"):
                    return {"data": page1, "nextCursor": "p2"}
                return {"data": page2, "nextCursor": None}
            return {}

        fake_client.call.side_effect = fake_call
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_client):
            res = bridge.op_wait_turn(
                {"thread_id": "tid", "turn_id": "t-1", "follow_new_turns": False, "timeout_seconds": 5},
                {},
            )
            self.assertEqual(res["waitOutcome"], "completed")
            self.assertEqual(res["turn"]["id"], "t-1")

    def test_wait_turn_does_not_false_trigger_user_steered_on_empty_initial_poll(self) -> None:
        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.call.side_effect = [
            # Poll 1: turn just started, items still empty
            {"thread": {"id": "tid", "status": {"type": "active", "activeFlags": []}}},
            {"data": [{"id": "turn-1", "status": "inProgress", "items": []}]},
            # Poll 2: initial userMessage appears (NOT a steer!)
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
            # Poll 3: completed cleanly
            {"thread": {"id": "tid", "status": {"type": "idle"}}},
            {
                "data": [
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "items": [
                            {"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": "go"}]},
                            {"type": "agentMessage", "id": "a1", "text": "done"},
                        ],
                    }
                ]
            },
            # Final hydration
            {
                "data": [
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "items": [
                            {"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": "go"}]},
                            {"type": "agentMessage", "id": "a1", "text": "done"},
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
            self.assertEqual(res["waitOutcome"], "completed")
            self.assertEqual(res["userInterventions"], [])

    def test_wait_turn_surfaces_mid_turn_steers_and_follows_follow_up_turn(self) -> None:
        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.call.side_effect = [
            {"thread": {"id": "tid", "status": {"type": "active", "activeFlags": []}}},
            {
                "data": [
                    {"id": "turn-2", "status": "inProgress", "items": []},
                    {"id": "turn-1", "status": "completed", "items": []},
                ]
            },
            {"thread": {"id": "tid", "status": {"type": "idle"}}},
            {
                "data": [
                    {"id": "turn-2", "status": "completed", "items": []},
                    {"id": "turn-1", "status": "completed", "items": []},
                ]
            },
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
                [(x["kind"], x["text"], x["source"]) for x in res["userInterventions"]],
                [
                    ("mid_turn_steer", "actually use option B", "external_or_unknown"),
                    ("follow_up_turn", "also add a test", "external_or_unknown"),
                ],
            )

    def test_wait_turn_can_return_early_on_user_steer_when_enabled(self) -> None:
        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.call.side_effect = [
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

    def test_start_thread_ephemeral_validation_and_partial_success_id_preservation(self) -> None:
        with self.assertRaises(client_mod.CodexBridgeError) as ctx1:
            bridge.op_start_thread({"cwd": REPO_ROOT, "ephemeral": True, "name": "temp"}, {})
        self.assertIn("ephemeral=true", str(ctx1.exception))

        with self.assertRaises(client_mod.CodexBridgeError) as ctx2:
            bridge.op_start_thread({"cwd": REPO_ROOT, "ephemeral": True, "prompt": "hi", "wait": True}, {})
        self.assertIn("ephemeral=true", str(ctx2.exception))

        # Valid ephemeral thread omits resumeCommand
        fake_eph = mock.MagicMock()
        fake_eph.__enter__.return_value = fake_eph
        fake_eph.call.return_value = {"thread": {"id": "tid-eph"}, "cwd": REPO_ROOT}
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_eph):
            res = bridge.op_start_thread({"cwd": REPO_ROOT, "ephemeral": True}, {})
            self.assertTrue(res["ephemeral"])
            self.assertFalse(res["resumable"])
            self.assertNotIn("resumeCommand", res)

        # Post-start failure preserves threadId via CodexPartialSuccessError
        fake_partial = mock.MagicMock()
        fake_partial.__enter__.return_value = fake_partial
        fake_partial.call.side_effect = [
            {"thread": {"id": "tid-created"}, "cwd": REPO_ROOT},
            client_mod.CodexRpcError({"code": -32603, "message": "boom during turn/start"}),
        ]
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_partial):
            with self.assertRaises(bridge.CodexPartialSuccessError) as pctx:
                bridge.op_start_thread({"cwd": REPO_ROOT, "prompt": "hi"}, {})
            self.assertTrue(pctx.exception.partial_result["partialSuccess"])
            self.assertEqual(pctx.exception.partial_result["threadId"], "tid-created")
            self.assertEqual(pctx.exception.partial_result["failedStage"], "turn/start")

    def test_remote_cwd_skips_local_isdir_and_formats_contextual_resume_command(self) -> None:
        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.call.return_value = {"thread": {"id": "tid-rem"}, "cwd": "/remote/only/repo"}
        conn_kw = {"remote_url": "wss://codex.example.com:8443/ws"}
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_client):
            res = bridge.op_start_thread({"cwd": "/remote/only/repo"}, conn_kw)
            self.assertEqual(res["cwd"], "/remote/only/repo")
            self.assertIn("--remote wss://codex.example.com:8443/ws", res["resumeCommand"])

    def test_start_thread_and_review_propagate_effort_and_service_tier(self) -> None:
        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.call.return_value = {
            "thread": {"id": "tid-new"},
            "cwd": REPO_ROOT,
            "model": "codex-test-model",
            "reasoningEffort": "xhigh",
            "serviceTier": "priority",
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

        fake_rev = mock.MagicMock()
        fake_rev.__enter__.return_value = fake_rev
        fake_rev.call.side_effect = [
            {
                "thread": {"id": "tid-rev"},
                "model": "codex-test-model",
                "reasoningEffort": "xhigh",
                "serviceTier": "priority",
            },
            {},
            {"turn": {"id": "rev-1", "status": "inProgress"}, "reviewThreadId": "tid-rev"},
        ]
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_rev):
            res2 = bridge.op_start_review(
                {"cwd": REPO_ROOT, "model": "codex-test-model", "effort": "xhigh", "service_tier": "fast"},
                {},
            )
            self.assertEqual(res2["threadConfig"]["model"], "codex-test-model")
            self.assertEqual(res2["threadConfig"]["reasoningEffort"], "xhigh")
            self.assertEqual(res2["threadConfig"]["serviceTier"], "priority")
            self.assertIn("reviewerModelNote", res2)
            first_method, first_params = fake_rev.call.call_args_list[0][0]
            self.assertEqual(first_method, "thread/start")
            self.assertEqual(first_params["serviceTier"], "priority")
            self.assertEqual(first_params["config"], {"model_reasoning_effort": "xhigh"})

    def test_list_threads_loaded_only_old_server_and_over_100_ids_and_archived_only(self) -> None:
        # Case 1: older server ignores includedThreadIds -> client still filters to loaded only
        fake_old = mock.MagicMock()
        fake_old.__enter__.return_value = fake_old
        fake_old.call.side_effect = [
            {"data": ["t-loaded"], "nextCursor": None},
            {
                "data": [
                    {"id": "t-not-loaded", "preview": "x"},
                    {"id": "t-loaded", "preview": "y"},
                ],
                "nextCursor": None,
            },
        ]
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_old):
            res = bridge.op_list_threads({"loaded_only": True, "archived_only": True, "limit": 5}, {})
            self.assertEqual([t["id"] for t in res["threads"]], ["t-loaded"])
            self.assertTrue(res["threads"][0]["isLoaded"])
            list_params = fake_old.call.call_args_list[1][0][1]
            self.assertTrue(list_params["archived"])
            self.assertTrue(list_params["useStateDbOnly"])

        # Case 2: 105 loaded IDs (>100 limit) -> omits includedThreadIds & filters client-side
        many_ids = [f"t-{i}" for i in range(105)]
        fake_many = mock.MagicMock()
        fake_many.__enter__.return_value = fake_many
        fake_many.call.side_effect = [
            {"data": many_ids, "nextCursor": None},
            {"data": [{"id": "t-other"}, {"id": "t-104"}], "nextCursor": None},
        ]
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_many):
            res2 = bridge.op_list_threads({"loaded_only": True, "limit": 5}, {})
            self.assertEqual([t["id"] for t in res2["threads"]], ["t-104"])
            list_params2 = fake_many.call.call_args_list[1][0][1]
            self.assertNotIn("includedThreadIds", list_params2)

    def test_get_turn_details_unwraps_entries_propagates_cursor_error_and_limits_fallback(self) -> None:
        fake_client = mock.MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.call.return_value = {
            "data": [
                {
                    "turnId": "turn-7",
                    "startedAtMs": 1000,
                    "completedAtMs": 1500,
                    "item": {
                        "type": "exitedReviewMode",
                        "id": "rev-1",
                        "review": "Found 2 critical issues in foo.py",
                    },
                },
                {
                    "turnId": "turn-7",
                    "item": {
                        "type": "agentMessage",
                        "id": "a-1",
                        "text": "Pick one",
                        "phase": "final_answer",
                        "questions": [{"title": "Option A or B?", "options": ["A", "B"]}],
                    },
                },
            ],
            "nextCursor": None,
        }
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_client):
            res = bridge.op_get_turn_details({"thread_id": "tid", "turn_id": "turn-7"}, {})
            self.assertEqual(res["count"], 2)
            self.assertEqual(res["items"][0]["startedAtMs"], 1000)
            self.assertEqual(res["items"][0]["completedAtMs"], 1500)
            self.assertEqual(res["items"][1]["questions"][0]["title"], "Option A or B?")

        # Invalid cursor error MUST NOT be swallowed into empty success
        fake_bad_cursor = mock.MagicMock()
        fake_bad_cursor.__enter__.return_value = fake_bad_cursor
        fake_bad_cursor.call.side_effect = client_mod.CodexRpcError(
            {"code": -32602, "message": "invalid cursor"}
        )
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_bad_cursor):
            with self.assertRaises(client_mod.CodexRpcError):
                bridge.op_get_turn_details(
                    {"thread_id": "tid", "turn_id": "turn-7", "cursor": "stale"}, {}
                )

        # Fallback on older daemon (-32601) respects caller's `limit`
        fake_fb = mock.MagicMock()
        fake_fb.__enter__.return_value = fake_fb
        fake_fb.call.side_effect = [
            client_mod.CodexRpcError({"code": -32601, "message": "thread/items/list is not supported yet"}),
            {
                "data": [
                    {
                        "id": "turn-live",
                        "status": "completed",
                        "items": [
                            {"type": "agentMessage", "id": "a1", "text": "one"},
                            {"type": "agentMessage", "id": "a2", "text": "two"},
                            {"type": "agentMessage", "id": "a3", "text": "three"},
                        ],
                    }
                ]
            },
        ]
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_fb):
            res2 = bridge.op_get_turn_details(
                {"thread_id": "tid", "turn_id": "turn-live", "limit": 1}, {}
            )
            self.assertEqual(res2["source"], "turns_list_fallback")
            self.assertEqual(res2["count"], 1)
            self.assertTrue(res2["truncatedByLimit"])

    def test_queue_never_retries_after_enqueue_and_surfaces_resume_warning(self) -> None:
        fake_q = mock.MagicMock()
        fake_q.__enter__.return_value = fake_q
        fake_q.call.side_effect = [
            {"queuedSubmission": {"id": "q-1"}},
            client_mod.CodexRpcError({"code": -32600, "message": "thread not found"}),
        ]
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_q):
            res = bridge.op_queue_message(
                {
                    "thread_id": "t1",
                    "message": "next",
                    "resume": True,
                    "confirm_modify_thread": True,
                },
                {},
            )
            self.assertTrue(res["queued"])
            self.assertEqual(res["queuedSubmission"], {"id": "q-1"})
            self.assertFalse(res["resumed"])
            self.assertIn("warning", res)
            # Only 2 RPC calls total (queue/add + thread/read), NEVER a retry
            self.assertEqual(fake_q.call.call_count, 2)

        # Unsupported daemon (-32601) raises clear upgrade error
        fake_q2 = mock.MagicMock()
        fake_q2.__enter__.return_value = fake_q2
        fake_q2.call.side_effect = client_mod.CodexRpcError(
            {"code": -32601, "message": "Method not found"}
        )
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_q2):
            with self.assertRaises(client_mod.CodexBridgeError) as ctx:
                bridge.op_queue_message(
                    {"thread_id": "t1", "message": "next", "confirm_modify_thread": True}, {}
                )
            self.assertIn("thread/queue/add", str(ctx.exception))

    def test_interrupt_reports_acknowledged_and_actual_turn_status(self) -> None:
        fake_int = mock.MagicMock()
        fake_int.__enter__.return_value = fake_int
        fake_int.call.side_effect = [
            {"thread": {"id": "t1", "status": {"type": "active", "activeFlags": []}}},
            {},
            {"data": [{"id": "turn-1", "status": "completed"}]},
        ]
        with mock.patch.object(bridge, "CodexAppClient", return_value=fake_int):
            res = bridge.op_interrupt_turn(
                {"thread_id": "t1", "turn_id": "turn-1", "confirm_modify_thread": True}, {}
            )
            self.assertTrue(res["interruptAcknowledged"])
            self.assertEqual(res["turnStatus"], "completed")
            self.assertFalse(res["interrupted"])

    def test_cli_turn_parses_limit_and_cursor(self) -> None:
        parser = bridge.build_cli_parser()
        ns = parser.parse_args(["turn", "tid", "turn-1", "--limit", "25", "--cursor", "cur-9"])
        self.assertEqual(ns.limit, 25)
        self.assertEqual(ns.cursor, "cur-9")


class McpProtocolTests(unittest.TestCase):
    def test_mcp_initialize_version_negotiation_and_validation(self) -> None:
        server = bridge.McpStdioServer({})
        # Unknown version falls back to highest supported (2025-06-18)
        init_unknown = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2099-01-01"},
            }
        )
        assert init_unknown is not None
        self.assertEqual(init_unknown["result"]["protocolVersion"], "2025-06-18")

        # Initialize without id (notification) returns None
        self.assertIsNone(
            server.handle_request({"jsonrpc": "2.0", "method": "initialize", "params": {}})
        )

        # Unknown tool -> JSON-RPC -32602
        unk = server.handle_request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "no_such_tool"}}
        )
        assert unk is not None
        self.assertEqual(unk["error"]["code"], -32602)

        # String "false" for boolean param -> JSON-RPC -32602 (never coerced to True!)
        bad_bool = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "codex_read_thread",
                    "arguments": {"thread_id": "t1", "include_tool_outputs": "false"},
                },
            }
        )
        assert bad_bool is not None
        self.assertEqual(bad_bool["error"]["code"], -32602)

        # Batch rejected cleanly in 2025-06-18
        batch_err = server.handle_message(
            [{"jsonrpc": "2.0", "id": 4, "method": "ping"}]
        )
        assert isinstance(batch_err, dict)
        self.assertEqual(batch_err["error"]["code"], -32600)

    def test_mcp_2025_03_26_batch_and_parse_error_over_real_subprocess(self) -> None:
        probe = [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "initialize",
                    "id": 1,
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
                }
            ),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps(
                [
                    {"jsonrpc": "2.0", "id": 2, "method": "ping"},
                    {"jsonrpc": "2.0", "id": 3, "method": "ping"},
                ]
            ),
            "{not valid json",
        ]
        p = subprocess.run(
            [sys.executable, "-B", os.path.join(SCRIPTS_DIR, "mcp_hot_reload.py")],
            input="\n".join(probe) + "\n",
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = [json.loads(line) for line in p.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertIsInstance(lines[1], list)
        self.assertEqual([r["id"] for r in lines[1]], [2, 3])
        self.assertEqual(lines[2]["error"]["code"], -32700)

    def test_mcp_concurrent_ping_and_cancellation_unblock_slow_tool(self) -> None:
        started = threading.Event()
        cancelled_observed = threading.Event()

        def slow_handler(args, conn_kw):
            started.set()
            try:
                bridge._sleep_cancellable(10.0)
            except client_mod.CodexCancelledError:
                cancelled_observed.set()
                raise
            return {"done": True}

        r_fd, w_fd = os.pipe()
        out_buf = io.BytesIO()
        srv = bridge.McpStdioServer({})

        def run_server() -> None:
            with os.fdopen(r_fd, "rb") as in_f:
                with mock.patch.object(sys, "stdin", mock.Mock(buffer=in_f)), mock.patch.object(
                    sys, "stdout", mock.Mock(buffer=out_buf)
                ), mock.patch.dict(
                    bridge.TOOL_MAP,
                    {
                        "codex_status": {
                            "name": "codex_status",
                            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                            "handler": slow_handler,
                        }
                    },
                ):
                    srv.serve_forever()

        t = threading.Thread(target=run_server, daemon=True)
        t.start()
        try:
            with os.fdopen(w_fd, "wb") as out_f:
                out_f.write(
                    (json.dumps({"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "codex_status", "arguments": {}}}) + "\n").encode()
                )
                out_f.flush()
                self.assertTrue(started.wait(timeout=3.0))

                # Concurrent ping must answer immediately while tool 10 is waiting!
                out_f.write((json.dumps({"jsonrpc": "2.0", "id": 11, "method": "ping"}) + "\n").encode())
                # Cancel tool 10!
                out_f.write(
                    (json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 10}}) + "\n").encode()
                )
                out_f.flush()
                self.assertTrue(cancelled_observed.wait(timeout=3.0))
        finally:
            t.join(timeout=3.0)

        responses = [json.loads(line) for line in out_buf.getvalue().decode("utf-8").splitlines() if line.strip()]
        # Ping (id=11) succeeded; cancelled call (id=10) emitted no late response
        self.assertEqual(responses, [{"jsonrpc": "2.0", "id": 11, "result": {}}])

    def test_mcp_jsonl_and_content_length_framing(self) -> None:
        req = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}).encode("utf-8")

        in_buf = io.BytesIO(req + b"\n")
        out_buf = io.BytesIO()
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=in_buf)), mock.patch.object(
            sys, "stdout", mock.Mock(buffer=out_buf)
        ):
            bridge.McpStdioServer({}).serve_forever()
        out_msg = json.loads(out_buf.getvalue().decode("utf-8"))
        self.assertEqual(out_msg, {"jsonrpc": "2.0", "id": 7, "result": {}})

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
    def test_atomic_hot_reload_survives_syntax_and_mid_init_mutation_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client_name = "_tmp_hr_client_atomic"
            bridge_name = "_tmp_hr_bridge_atomic"
            client_path = os.path.join(tmp, f"{client_name}.py")
            bridge_path = os.path.join(tmp, f"{bridge_name}.py")

            def write_pair(
                val: str,
                extra_tool: bool = False,
                broken_syntax: bool = False,
                mutating_crash: bool = False,
                step: int = 0,
            ) -> None:
                with open(client_path, "w", encoding="utf-8") as f:
                    f.write(f"VALUE = {val!r}\n")
                with open(bridge_path, "w", encoding="utf-8") as f:
                    if broken_syntax:
                        f.write("def broken(:\n")
                    elif mutating_crash:
                        # Mutates module global BEFORE crashing; isolated reload MUST NOT corrupt active v2!
                        f.write(
                            f"from {client_name} import VALUE\n"
                            "GLOBAL_STATE = 'corrupted'\n"
                            "raise RuntimeError('mid-import failure')\n"
                        )
                    else:
                        tools_expr = '[{"name": "t1"}]' if not extra_tool else '[{"name": "t1"}, {"name": "t2"}]'
                        f.write(
                            f"from {client_name} import VALUE\n"
                            f"GLOBAL_STATE = {val!r}\n"
                            "class McpStdioServer:\n"
                            "    def __init__(self, conn_kw):\n"
                            "        self.framing = 'jsonl'\n"
                            "        self.negotiated_protocol = None\n"
                            "    def handle_request(self, req, cancel_event=None):\n"
                            "        m = req.get('method')\n"
                            "        if m == 'initialize':\n"
                            "            return {'jsonrpc':'2.0','id':req['id'],'result':{'protocolVersion':'2025-06-18','capabilities':{'tools':{}}}}\n"
                            "        if m == 'tools/list':\n"
                            f"            return {{'jsonrpc':'2.0','id':req['id'],'result':{{'tools':{tools_expr}}}}}\n"
                            "        if m == 'tools/call':\n"
                            "            return {'jsonrpc':'2.0','id':req['id'],'result':{'value': VALUE, 'state': GLOBAL_STATE}}\n"
                            "        return None\n"
                        )
                future = time.time_ns() + step * 5_000_000_000
                os.utime(client_path, ns=(future, future))
                os.utime(bridge_path, ns=(future, future))

            write_pair("v1", step=0)
            sys.path.insert(0, tmp)
            try:
                srv = hot_reload.HotReloadMcpServer(
                    {}, client_module_name=client_name, bridge_module_name=bridge_name
                )
                init_msgs = srv.process_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
                self.assertTrue(init_msgs[0]["result"]["capabilities"]["tools"]["listChanged"])

                r1 = srv.process_request({"jsonrpc": "2.0", "id": 2, "method": "tools/call"})
                self.assertEqual(r1[0]["result"], {"value": "v1", "state": "v1"})

                # Update both modules -> atomic swap + list_changed
                write_pair("v2", extra_tool=True, step=1)
                r2 = srv.process_request({"jsonrpc": "2.0", "id": 3, "method": "tools/call"})
                self.assertEqual(r2[0]["result"], {"value": "v2", "state": "v2"})
                self.assertEqual(len(r2), 2)
                self.assertEqual(r2[1]["method"], "notifications/tools/list_changed")

                # Mid-import crash after mutating globals -> active v2 remains 100% untouched!
                write_pair("v3-bad", mutating_crash=True, step=2)
                r3 = srv.process_request({"jsonrpc": "2.0", "id": 4, "method": "tools/call"})
                self.assertEqual(r3[0]["result"], {"value": "v2", "state": "v2"})

                # Syntax error -> active v2 still untouched!
                write_pair("v4-syntax", broken_syntax=True, step=3)
                r4 = srv.process_request({"jsonrpc": "2.0", "id": 5, "method": "tools/call"})
                self.assertEqual(r4[0]["result"], {"value": "v2", "state": "v2"})
            finally:
                sys.path.remove(tmp)
                sys.modules.pop(client_name, None)
                sys.modules.pop(bridge_name, None)


if __name__ == "__main__":
    unittest.main()
