#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import math
import os
import shlex
import sys
import threading
import time
import uuid
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from codex_app_client import (  # noqa: E402
    CLIENT_NAME,
    CLIENT_VERSION,
    CodexAppClient,
    CodexBridgeError,
    CodexCancelledError,
    CodexRpcError,
    find_codex_binary,
)

TERMINAL_TURN_STATUSES = {"completed", "failed", "interrupted"}
SUPPORTED_MCP_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_WAIT_TIMEOUT_SECONDS = 120.0
MAX_WAIT_TIMEOUT_SECONDS = 600.0
MAX_TOTAL_TOOL_OUTPUT_CHARS = 120_000
MAX_FINAL_ANSWER_COMPACT_CHARS = 24_000
MAX_FINAL_ANSWER_FULL_CHARS = 50_000
MAX_COMMENTARY_COMPACT_CHARS = 8_000
MAX_LOADED_FILTER_IDS = 100

_CANCEL_CTX = threading.local()
_OWN_CLIENT_MSG_IDS_LOCK = threading.Lock()
_OWN_CLIENT_MSG_IDS: Deque[str] = collections.deque(maxlen=512)


class CodexPartialSuccessError(CodexBridgeError):
    """Raised when a multi-step write operation partially succeeded before failing."""

    def __init__(self, message: str, partial_result: Dict[str, Any]):
        super().__init__(message)
        self.partial_result = partial_result


class McpProtocolError(Exception):
    """Raised for JSON-RPC / MCP protocol-level errors (-32700/-32600/-32602)."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _OutputBudget:
    """Shared character budget across heavy tool outputs / diffs in one response."""

    def __init__(self, total_limit: int = MAX_TOTAL_TOOL_OUTPUT_CHARS):
        self.total_limit = max(1000, int(total_limit))
        self.remaining = self.total_limit
        self.exhausted = False

    def allocate(self, text: str, per_item_limit: int) -> Tuple[str, bool, bool]:
        """Return `(text, truncated, budget_exhausted)` within remaining total budget."""
        if not text:
            return "", False, False
        if self.remaining <= 0:
            self.exhausted = True
            return (
                f"... [omitted {len(text)} chars: response output budget exhausted]",
                True,
                True,
            )
        effective_limit = min(per_item_limit, self.remaining)
        out, truncated = _truncate(text, effective_limit)
        used = min(len(text), effective_limit)
        self.remaining = max(0, self.remaining - used)
        budget_hit = truncated and effective_limit < min(len(text), per_item_limit)
        if budget_hit or self.remaining == 0 and truncated:
            self.exhausted = True
        return out, truncated, budget_hit


def _record_own_client_message_id(client_msg_id: str) -> None:
    with _OWN_CLIENT_MSG_IDS_LOCK:
        _OWN_CLIENT_MSG_IDS.append(client_msg_id)


def _is_own_client_message_id(client_msg_id: Optional[str]) -> bool:
    if not client_msg_id:
        return False
    with _OWN_CLIENT_MSG_IDS_LOCK:
        return client_msg_id in _OWN_CLIENT_MSG_IDS


def _get_cancel_event() -> Optional[threading.Event]:
    return getattr(_CANCEL_CTX, "event", None)


def _check_cancelled(cancel_event: Optional[threading.Event] = None) -> None:
    ev = cancel_event if cancel_event is not None else _get_cancel_event()
    if ev is not None and ev.is_set():
        raise CodexCancelledError("Operation cancelled by client")


def _sleep_cancellable(
    seconds: float, cancel_event: Optional[threading.Event] = None
) -> None:
    ev = cancel_event if cancel_event is not None else _get_cancel_event()
    if ev is None:
        time.sleep(max(0.0, seconds))
        return
    if ev.wait(timeout=max(0.0, seconds)):
        raise CodexCancelledError("Operation cancelled by client")


def _open_client(
    conn_kw: Dict[str, Any],
    *,
    rpc_timeout: Optional[float] = None,
    connect_timeout: Optional[float] = None,
    cancel_event: Optional[threading.Event] = None,
) -> CodexAppClient:
    kwargs = dict(conn_kw)
    ev = cancel_event if cancel_event is not None else _get_cancel_event()
    if rpc_timeout is not None:
        kwargs["rpc_timeout"] = rpc_timeout
    if connect_timeout is not None:
        kwargs["connect_timeout"] = connect_timeout
    if ev is not None:
        kwargs["cancel_event"] = ev
    return CodexAppClient(**kwargs)


def _effective_remote_url(conn_kw: Dict[str, Any]) -> Optional[str]:
    return conn_kw.get("remote_url") or os.environ.get("CODEX_REMOTE_URL")


def _effective_sock_path(conn_kw: Dict[str, Any]) -> Optional[str]:
    return conn_kw.get("sock_path") or os.environ.get("CODEX_APP_SERVER_SOCK")


def _is_remote_connection(conn_kw: Dict[str, Any]) -> bool:
    remote_url = _effective_remote_url(conn_kw)
    if not remote_url:
        return False
    scheme = urlparse(remote_url).scheme.lower()
    return scheme in ("ws", "wss")


def _build_resume_command(thread_id: str, conn_kw: Dict[str, Any]) -> str:
    remote_url = _effective_remote_url(conn_kw)
    if remote_url:
        return f"codex --remote {shlex.quote(remote_url)} resume {shlex.quote(thread_id)}"
    sock_path = _effective_sock_path(conn_kw)
    if sock_path:
        return f"CODEX_APP_SERVER_SOCK={shlex.quote(sock_path)} codex resume {shlex.quote(thread_id)}"
    return f"codex resume {thread_id}"


def _resolve_cwd_arg(raw_cwd: str, conn_kw: Dict[str, Any]) -> str:
    cwd_str = str(raw_cwd).strip()
    if not cwd_str:
        raise CodexBridgeError("`cwd` must be a non-empty path")
    if _is_remote_connection(conn_kw):
        # Do not resolve or stat remote filesystem paths on the local host.
        return cwd_str
    cwd = os.path.abspath(os.path.expanduser(cwd_str))
    if not os.path.isdir(cwd):
        raise CodexBridgeError(f"cwd does not exist or is not a directory: {cwd}")
    return cwd


def _truncate(text: Optional[str], limit: int) -> Tuple[str, bool]:
    if not text:
        return "", False
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]", True


def _iso_ts(ts: Any) -> Optional[str]:
    if ts is None:
        return None
    try:
        val = float(ts)
        if val > 1e12:
            val = val / 1000.0
        return dt.datetime.fromtimestamp(val, tz=dt.timezone.utc).astimezone().isoformat(
            timespec="seconds"
        )
    except Exception:
        return str(ts)


def _extract_user_message_text(item: Dict[str, Any]) -> str:
    parts: List[str] = []
    for block in item.get("content") or []:
        if isinstance(block, dict):
            if block.get("text"):
                parts.append(str(block["text"]))
            elif block.get("type") == "image":
                parts.append("[image]")
            elif block.get("type"):
                parts.append(f"[{block['type']}]")
    return "\n".join(parts)


def _user_message_client_id(item: Dict[str, Any]) -> Optional[str]:
    for key in ("clientUserMessageId", "clientId", "clientMessageId"):
        val = item.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _extract_user_interventions(turns_asc: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collect mid-turn steer messages and follow-up turn user messages."""
    interventions: List[Dict[str, Any]] = []
    for turn_idx, tr in enumerate(turns_asc):
        tid = tr.get("id")
        um_idx = 0
        for it in tr.get("items") or []:
            if not isinstance(it, dict) or it.get("type") != "userMessage":
                continue
            text, truncated = _truncate(_extract_user_message_text(it), 4000)
            client_id = _user_message_client_id(it)
            source = "bridge" if _is_own_client_message_id(client_id) else "external_or_unknown"
            if turn_idx == 0 and um_idx > 0:
                entry: Dict[str, Any] = {
                    "kind": "mid_turn_steer",
                    "turnId": tid,
                    "itemId": it.get("id"),
                    "source": source,
                    "text": text,
                }
                if client_id:
                    entry["clientId"] = client_id
                if truncated:
                    entry["truncated"] = True
                interventions.append(entry)
            elif turn_idx > 0:
                entry = {
                    "kind": "follow_up_turn",
                    "turnId": tid,
                    "itemId": it.get("id"),
                    "source": source,
                    "text": text,
                }
                if client_id:
                    entry["clientId"] = client_id
                if truncated:
                    entry["truncated"] = True
                interventions.append(entry)
            um_idx += 1
    return interventions


def _normalize_service_tier(value: Any) -> Optional[str]:
    """Normalize user-friendly service tier aliases to Codex's canonical wire ID.

    Codex's catalog advertises Fast mode under id `"priority"` (and
    `serviceTierForTurn` does not normalize legacy `"fast"` on the server).
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.lower() == "fast":
        return "priority"
    return raw


def _resolve_rpc_items_view(items_view: str, include_tool_outputs: bool) -> str:
    """Map bridge items_view to Codex app-server TurnItemsView.

    `compact` (the default) fetches `full` over the local socket and compacts
    heavy tool outputs in Python so mid-turn steered userMessages are never
    dropped by Codex's server-side display summary.
    """
    if include_tool_outputs:
        return "full"
    if items_view == "compact":
        return "full"
    if items_view in ("notLoaded", "summary", "full"):
        return items_view
    return "full"


def _apply_agent_message_moderation(
    item: Dict[str, Any], raw_text: str
) -> Tuple[str, Optional[Dict[str, Any]], bool]:
    """Honor Codex `agentMessage.moderation.display` restrictions before exposing text.

    Matches `codex-rs/protocol/src/items/display.rs` (`Show`, `Hide`, `Block`,
    and unreadable/restricted fallback when `moderated=true`).
    """
    moderation = item.get("moderation")
    if not isinstance(moderation, dict):
        return raw_text, None, False

    display = moderation.get("display")
    moderated = bool(moderation.get("moderated"))
    if isinstance(display, dict):
        mode = display.get("mode")
        if mode == "show":
            return raw_text, (display if display.get("annotation") else None), False
        if mode == "hide":
            return "[Message hidden by Codex moderation display policy]", display, True
        if mode == "block":
            return "[Message blocked by Codex moderation display policy]", display, True
        return (
            "[Message withheld: unrecognized Codex moderation display policy]",
            display,
            True,
        )

    if moderated:
        return (
            "[Message withheld by Codex moderation policy]",
            {"moderated": True},
            True,
        )
    return raw_text, None, False


def format_item(
    item: Dict[str, Any],
    *,
    include_tool_outputs: bool = False,
    max_output_chars: int = 8000,
    budget: Optional[_OutputBudget] = None,
) -> Optional[Dict[str, Any]]:
    """Format a Codex ThreadItem cleanly while keeping token cost tight."""
    item_type = item.get("type", "unknown")
    ts_extra: Dict[str, Any] = {}
    for ts_key in ("startedAtMs", "completedAtMs"):
        if item.get(ts_key) is not None:
            ts_extra[ts_key] = item[ts_key]

    if item_type == "userMessage":
        text, truncated = _truncate(_extract_user_message_text(item), 6000)
        out: Dict[str, Any] = {"type": "userMessage", "id": item.get("id"), "text": text}
        client_id = _user_message_client_id(item)
        if client_id:
            out["clientId"] = client_id
        if truncated:
            out["truncated"] = True
        out.update(ts_extra)
        return out

    if item_type == "agentMessage":
        raw_text = str(item.get("text") or "")
        safe_text, mod_display, withheld = _apply_agent_message_moderation(item, raw_text)
        phase = item.get("phase")
        if withheld:
            text, truncated = safe_text, False
        elif phase == "final_answer":
            limit = (
                max(max_output_chars, MAX_FINAL_ANSWER_COMPACT_CHARS)
                if include_tool_outputs
                else MAX_FINAL_ANSWER_COMPACT_CHARS
            )
            limit = min(limit, MAX_FINAL_ANSWER_FULL_CHARS)
            text, truncated = _truncate(safe_text, limit)
        else:
            limit = max_output_chars if include_tool_outputs else MAX_COMMENTARY_COMPACT_CHARS
            text, truncated = _truncate(safe_text, limit)

        out = {"type": "agentMessage", "id": item.get("id"), "text": text}
        if phase is not None:
            out["phase"] = phase
        if item.get("questions") and not withheld:
            out["questions"] = item.get("questions")
        if mod_display is not None:
            out["moderationDisplay"] = mod_display
        if withheld:
            out["textWithheld"] = True
        if truncated:
            out["truncated"] = True
        out.update(ts_extra)
        return out

    if item_type in ("enteredReviewMode", "exitedReviewMode"):
        review_limit = max_output_chars if include_tool_outputs else 12000
        review_text, truncated = _truncate(str(item.get("review") or ""), review_limit)
        out = {"type": item_type, "id": item.get("id"), "review": review_text}
        if truncated:
            out["truncated"] = True
        out.update(ts_extra)
        return out

    if item_type == "plan":
        plan_limit = max_output_chars if include_tool_outputs else 12000
        text, truncated = _truncate(str(item.get("text") or ""), plan_limit)
        out = {"type": "plan", "id": item.get("id"), "text": text}
        if truncated:
            out["truncated"] = True
        out.update(ts_extra)
        return out

    if item_type == "hookPrompt":
        frags = [
            str(f.get("text") or "")
            for f in (item.get("fragments") or [])
            if isinstance(f, dict) and f.get("text")
        ]
        text, truncated = _truncate("\n".join(frags), 2000)
        out = {"type": "hookPrompt", "id": item.get("id"), "text": text}
        if truncated:
            out["truncated"] = True
        out.update(ts_extra)
        return out

    if item_type == "reasoning":
        summary = [str(s) for s in (item.get("summary") or []) if str(s).strip()]
        if not summary:
            return None  # Drop empty reasoning placeholders to save tokens.
        out = {"type": "reasoning", "id": item.get("id"), "summary": summary}
        out.update(ts_extra)
        return out

    if item_type == "commandExecution":
        agg = str(item.get("aggregatedOutput") or "")
        out = {
            "type": "commandExecution",
            "id": item.get("id"),
            "command": item.get("command"),
            "cwd": item.get("cwd"),
            "status": item.get("status"),
            "exitCode": item.get("exitCode"),
            "durationMs": item.get("durationMs"),
            "outputChars": len(agg),
        }
        if include_tool_outputs and agg:
            if budget is not None:
                text, truncated, budget_hit = budget.allocate(agg, max_output_chars)
                if budget_hit:
                    out["budgetTruncated"] = True
            else:
                text, truncated = _truncate(agg, max_output_chars)
            out["output"] = text
            if truncated:
                out["truncated"] = True
        elif item.get("exitCode") not in (None, 0) and agg:
            text, truncated = _truncate(agg, 1200)
            out["outputPreview"] = text
            if truncated:
                out["truncated"] = True
        out.update(ts_extra)
        return out

    if item_type == "fileChange":
        changes = []
        for ch in item.get("changes") or []:
            if not isinstance(ch, dict):
                continue
            diff = str(ch.get("diff") or "")
            entry: Dict[str, Any] = {
                "path": ch.get("path"),
                "kind": ch.get("kind"),
                "diffChars": len(diff),
            }
            if include_tool_outputs and diff:
                if budget is not None:
                    text, truncated, budget_hit = budget.allocate(diff, max_output_chars)
                    if budget_hit:
                        entry["budgetTruncated"] = True
                else:
                    text, truncated = _truncate(diff, max_output_chars)
                entry["diff"] = text
                if truncated:
                    entry["truncated"] = True
            changes.append(entry)
        out = {
            "type": "fileChange",
            "id": item.get("id"),
            "status": item.get("status"),
            "changes": changes,
        }
        out.update(ts_extra)
        return out

    if item_type == "mcpToolCall":
        out = {
            "type": "mcpToolCall",
            "id": item.get("id"),
            "status": item.get("status"),
            "server": item.get("server"),
            "tool": item.get("tool") or item.get("name"),
            "durationMs": item.get("durationMs"),
        }
        if include_tool_outputs and item.get("arguments") is not None:
            raw_args = json.dumps(item.get("arguments"), ensure_ascii=False)
            if budget is not None:
                a_text, a_trunc, a_budget = budget.allocate(raw_args, min(max_output_chars, 4000))
                if a_budget:
                    out["budgetTruncated"] = True
            else:
                a_text, a_trunc = _truncate(raw_args, min(max_output_chars, 4000))
            out["arguments"] = a_text
            if a_trunc:
                out["argumentsTruncated"] = True
        if include_tool_outputs and item.get("result") is not None:
            raw_res = json.dumps(item.get("result"), ensure_ascii=False)
            if budget is not None:
                text, truncated, budget_hit = budget.allocate(raw_res, max_output_chars)
                if budget_hit:
                    out["budgetTruncated"] = True
            else:
                text, truncated = _truncate(raw_res, max_output_chars)
            out["result"] = text
            if truncated:
                out["truncated"] = True
        if item.get("error") is not None:
            out["error"] = item.get("error")
        out.update(ts_extra)
        return {k: v for k, v in out.items() if v is not None}

    if item_type == "collabAgentToolCall":
        out = {
            "type": "collabAgentToolCall",
            "id": item.get("id"),
            "tool": item.get("tool"),
            "status": item.get("status"),
            "senderThreadId": item.get("senderThreadId"),
            "receiverThreadIds": item.get("receiverThreadIds"),
            "model": item.get("model"),
            "reasoningEffort": item.get("reasoningEffort"),
            "agentsStates": item.get("agentsStates"),
        }
        if item.get("prompt"):
            p_text, p_trunc = _truncate(str(item["prompt"]), 1000)
            out["prompt"] = p_text
            if p_trunc:
                out["promptTruncated"] = True
        out.update(ts_extra)
        return {k: v for k, v in out.items() if v is not None}

    if item_type == "subAgentActivity":
        out = {
            "type": "subAgentActivity",
            "id": item.get("id"),
            "kind": item.get("kind"),
            "agentThreadId": item.get("agentThreadId"),
            "agentPath": item.get("agentPath"),
            "model": item.get("model"),
            "reasoningEffort": item.get("reasoningEffort"),
        }
        out.update(ts_extra)
        return {k: v for k, v in out.items() if v is not None}

    if item_type == "dynamicToolCall":
        out = {
            "type": "dynamicToolCall",
            "id": item.get("id"),
            "namespace": item.get("namespace"),
            "tool": item.get("tool"),
            "status": item.get("status"),
            "success": item.get("success"),
            "durationMs": item.get("durationMs"),
        }
        if include_tool_outputs and item.get("arguments") is not None:
            raw_args = json.dumps(item.get("arguments"), ensure_ascii=False)
            if budget is not None:
                a_text, a_trunc, a_budget = budget.allocate(raw_args, min(max_output_chars, 4000))
                if a_budget:
                    out["budgetTruncated"] = True
            else:
                a_text, a_trunc = _truncate(raw_args, min(max_output_chars, 4000))
            out["arguments"] = a_text
            if a_trunc:
                out["argumentsTruncated"] = True
        if include_tool_outputs and item.get("contentItems") is not None:
            raw_res = json.dumps(item.get("contentItems"), ensure_ascii=False)
            if budget is not None:
                text, truncated, budget_hit = budget.allocate(raw_res, max_output_chars)
                if budget_hit:
                    out["budgetTruncated"] = True
            else:
                text, truncated = _truncate(raw_res, max_output_chars)
            out["contentItems"] = text
            if truncated:
                out["truncated"] = True
        out.update(ts_extra)
        return {k: v for k, v in out.items() if v is not None}

    if item_type == "functionCallOutput":
        out = {
            "type": "functionCallOutput",
            "id": item.get("id"),
            "namespace": item.get("namespace"),
            "name": item.get("name"),
        }
        if include_tool_outputs and item.get("output") is not None:
            raw_out = json.dumps(item.get("output"), ensure_ascii=False)
            if budget is not None:
                text, truncated, budget_hit = budget.allocate(raw_out, max_output_chars)
                if budget_hit:
                    out["budgetTruncated"] = True
            else:
                text, truncated = _truncate(raw_out, max_output_chars)
            out["output"] = text
            if truncated:
                out["truncated"] = True
        out.update(ts_extra)
        return {k: v for k, v in out.items() if v is not None}

    if item_type == "webSearch":
        out = {
            "type": "webSearch",
            "id": item.get("id"),
            "query": item.get("query"),
            "action": item.get("action"),
            "status": item.get("status"),
        }
        out.update(ts_extra)
        return {k: v for k, v in out.items() if v is not None}

    # Generic fallback for contextCompaction, imageView, sleep, imageGeneration, etc.
    compact: Dict[str, Any] = {"type": item_type, "id": item.get("id")}
    for k in ("status", "name", "path", "text", "review", "summary", "error", "durationMs"):
        if item.get(k) is not None:
            val = item[k]
            if isinstance(val, str):
                val, _ = _truncate(val, 2000)
            compact[k] = val
    compact.update(ts_extra)
    return compact


def format_turn(
    turn: Dict[str, Any],
    *,
    include_tool_outputs: bool = False,
    max_output_chars: int = 8000,
    budget: Optional[_OutputBudget] = None,
) -> Dict[str, Any]:
    raw_items = turn.get("items") or []
    effective_budget = budget if budget is not None else _OutputBudget()
    formatted_items: List[Dict[str, Any]] = []
    omitted_empty_reasoning = 0
    user_messages: List[Dict[str, Any]] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        fmt = format_item(
            it,
            include_tool_outputs=include_tool_outputs,
            max_output_chars=max_output_chars,
            budget=effective_budget,
        )
        if fmt is None:
            omitted_empty_reasoning += 1
        else:
            formatted_items.append(fmt)
            if fmt.get("type") == "userMessage":
                user_messages.append(fmt)

    out: Dict[str, Any] = {
        "id": turn.get("id"),
        "status": turn.get("status"),
        "itemsView": turn.get("itemsView"),
        "startedAt": _iso_ts(turn.get("startedAt")),
        "completedAt": _iso_ts(turn.get("completedAt")),
        "durationMs": turn.get("durationMs"),
        "itemCount": len(raw_items),
        "items": formatted_items,
    }
    if len(user_messages) > 1:
        out["midTurnSteers"] = [
            {"itemId": um.get("id"), "text": um.get("text")}
            for um in user_messages[1:]
        ]
    if omitted_empty_reasoning:
        out["omittedEmptyReasoningCount"] = omitted_empty_reasoning
    if effective_budget.exhausted:
        out["outputBudgetExhausted"] = True
    if turn.get("error") is not None:
        out["error"] = turn.get("error")
    return out


def format_thread_summary(
    thread: Dict[str, Any],
    *,
    loaded_ids: Optional[Set[str]] = None,
    preview_chars: int = 180,
) -> Dict[str, Any]:
    tid = thread.get("id")
    preview, truncated = _truncate(str(thread.get("preview") or ""), preview_chars)
    status = thread.get("status")
    out: Dict[str, Any] = {
        "id": tid,
        "name": thread.get("name"),
        "status": status,
        "model": thread.get("model"),
        "reasoningEffort": thread.get("reasoningEffort"),
        "cwd": thread.get("cwd"),
        "source": thread.get("source"),
        "createdAt": _iso_ts(thread.get("createdAt")),
        "updatedAt": _iso_ts(thread.get("updatedAt")),
        "canAcceptDirectInput": thread.get("canAcceptDirectInput"),
        "preview": preview,
    }
    if truncated:
        out["previewTruncated"] = True
    if loaded_ids is not None:
        out["isLoaded"] = tid in loaded_ids
    if thread.get("gitInfo"):
        out["gitInfo"] = thread.get("gitInfo")
    if thread.get("forkedFromId"):
        out["forkedFromId"] = thread.get("forkedFromId")
    return out


def _require_modify_confirmation(args: Dict[str, Any], action: str) -> None:
    if args.get("confirm_modify_thread") is not True:
        raise CodexBridgeError(
            f"Safety guardrail: `{action}` modifies an existing Codex thread and requires "
            "`confirm_modify_thread=true`. Only pass true for threads created by AGY or "
            "when the user explicitly asked to interact with that existing thread."
        )


def _active_flags(thread: Dict[str, Any]) -> List[str]:
    status = thread.get("status")
    if isinstance(status, dict) and status.get("type") == "active":
        return list(status.get("activeFlags") or [])
    return []


def _extract_latest_turn(turns_res: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    turns = turns_res.get("data") or turns_res.get("turns") or []
    return turns[0] if turns else None


def _is_unmaterialized_thread_error(exc: CodexRpcError) -> bool:
    msg = str(exc.error.get("message") or "").lower()
    return (
        "no rollout found" in msg
        or "not materialized" in msg
        or "before the first" in msg
        or "no turns" in msg
    )


def _is_method_not_found_error(exc: CodexRpcError) -> bool:
    code = exc.error.get("code")
    msg = str(exc.error.get("message") or "").lower()
    return (
        code == -32601
        or "method not found" in msg
        or "unknown method" in msg
        or "is not supported" in msg
    )


def _ensure_loaded(
    client: CodexAppClient,
    thread_id: str,
    *,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    call_kw: Dict[str, Any] = {}
    if timeout is not None:
        call_kw["timeout"] = timeout
    meta = client.call(
        "thread/read", {"threadId": thread_id, "includeTurns": False}, **call_kw
    )
    thread = (meta or {}).get("thread") or {}
    status = thread.get("status")
    is_not_loaded = isinstance(status, dict) and status.get("type") == "notLoaded"
    if is_not_loaded:
        client.call(
            "thread/resume",
            {"threadId": thread_id, "excludeTurns": True},
            **call_kw,
        )
        meta = client.call(
            "thread/read", {"threadId": thread_id, "includeTurns": False}, **call_kw
        )
        thread = (meta or {}).get("thread") or {}
    return thread


def _list_all_loaded_thread_ids(
    client: CodexAppClient, *, max_pages: int = 200
) -> List[str]:
    """Fetch all currently loaded thread IDs across pagination cursors."""
    loaded_ids: List[str] = []
    seen: Set[str] = set()
    cursor: Optional[str] = None
    for _ in range(max_pages):
        params: Dict[str, Any] = {}
        if cursor is not None:
            params["cursor"] = cursor
        res = client.call("thread/loaded/list", params) or {}
        for tid in res.get("data") or []:
            if tid not in seen:
                seen.add(tid)
                loaded_ids.append(tid)
        next_cursor = res.get("nextCursor")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return loaded_ids


def _unwrap_item_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Unwrap `ThreadItemEntry` (`{turnId, item, startedAtMs, completedAtMs}`) returned by `thread/items/list`."""
    inner = entry.get("item")
    if isinstance(inner, dict):
        merged = dict(inner)
        for ts_key in ("startedAtMs", "completedAtMs"):
            if entry.get(ts_key) is not None and merged.get(ts_key) is None:
                merged[ts_key] = entry[ts_key]
        return merged
    return entry


def _slice_turn_window(
    turns_desc: List[Dict[str, Any]], anchor_turn_id: Optional[str]
) -> List[Dict[str, Any]]:
    """Return chronological turns from anchor_turn_id through the latest turn.

    When `anchor_turn_id` is explicitly provided but has not appeared in
    `turns_desc` yet, returns `[]` so waiters never mistake an older completed
    turn for the target turn.
    """
    if not turns_desc:
        return []
    if not anchor_turn_id:
        return [turns_desc[0]]
    collected: List[Dict[str, Any]] = []
    found = False
    for tr in turns_desc:
        collected.append(tr)
        if tr.get("id") == anchor_turn_id:
            found = True
            break
    if not found:
        return []
    collected.reverse()
    return collected


def _fetch_turns_until_anchor(
    client: CodexAppClient,
    thread_id: str,
    anchor_turn_id: Optional[str],
    *,
    items_view: str,
    page_limit: int = 20,
    max_pages: int = 10,
    deadline: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Fetch descending turns via cursor pagination until `anchor_turn_id` is found."""
    all_desc: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()
    cursor: Optional[str] = None
    for page_idx in range(max_pages):
        _check_cancelled()
        call_kw: Dict[str, Any] = {}
        if deadline is not None:
            rem = deadline - time.monotonic()
            if rem <= 0:
                break
            call_kw["timeout"] = min(25.0, max(0.25, rem))

        params: Dict[str, Any] = {
            "threadId": thread_id,
            "limit": page_limit,
            "sortDirection": "desc",
            "itemsView": items_view,
        }
        if cursor is not None:
            params["cursor"] = cursor
        res = client.call("thread/turns/list", params, **call_kw) or {}
        batch = res.get("data") or res.get("turns") or []
        for tr in batch:
            if not isinstance(tr, dict):
                continue
            tid = tr.get("id")
            if tid is not None and tid in seen_ids:
                continue
            if tid is not None:
                seen_ids.add(tid)
            all_desc.append(tr)
            if anchor_turn_id is not None and tid == anchor_turn_id:
                return all_desc, True
        if anchor_turn_id is None:
            return all_desc, bool(all_desc)
        next_cursor = res.get("nextCursor")
        if not next_cursor or next_cursor == cursor or not batch:
            break
        cursor = next_cursor
    found = any(tr.get("id") == anchor_turn_id for tr in all_desc) if anchor_turn_id else bool(all_desc)
    return all_desc, found


def _user_message_ids_in_turn(turn: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    for idx, it in enumerate(turn.get("items") or []):
        if isinstance(it, dict) and it.get("type") == "userMessage":
            ids.append(str(it.get("id") or f"idx:{idx}"))
    return ids


def _wait_for_turn_readonly(
    client: CodexAppClient,
    thread_id: str,
    *,
    turn_id: Optional[str] = None,
    timeout_seconds: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = 1.5,
    return_on_user_wait: bool = True,
    follow_new_turns: bool = True,
    return_on_user_steer: bool = False,
    include_tool_outputs: bool = False,
    deadline_override: Optional[float] = None,
) -> Dict[str, Any]:
    timeout_seconds = max(1.0, min(float(timeout_seconds), MAX_WAIT_TIMEOUT_SECONDS))
    poll_interval_seconds = max(0.5, min(float(poll_interval_seconds), 10.0))
    start = time.monotonic()
    deadline = deadline_override if deadline_override is not None else (start + timeout_seconds)
    anchor_turn_id = turn_id
    baseline_user_msg_ids: Optional[Set[str]] = None
    active_idle_grace_start: Optional[float] = None

    def _call_rem() -> Dict[str, Any]:
        rem = deadline - time.monotonic()
        if rem <= 0:
            return {"timeout": 0.25}
        return {"timeout": min(25.0, max(0.25, rem))}

    def _hydrate_turns(
        window_count: int,
        target_anchor_id: Optional[str],
        only_anchor: bool,
    ) -> List[Dict[str, Any]]:
        fetch_limit = max(window_count + 2, 10)
        full_desc, _ = _fetch_turns_until_anchor(
            client,
            thread_id,
            target_anchor_id,
            items_view="full",
            page_limit=min(fetch_limit, 50),
            max_pages=10,
            deadline=max(deadline, time.monotonic() + 2.0),
        )
        window = _slice_turn_window(full_desc, target_anchor_id)
        if only_anchor and target_anchor_id is not None:
            window = [tr for tr in window if tr.get("id") == target_anchor_id]
        return window

    def _build_result(
        outcome: str,
        thread: Dict[str, Any],
        final_window: List[Dict[str, Any]],
        *,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        budget = _OutputBudget()
        interventions = _extract_user_interventions(final_window)
        formatted_turns = [
            format_turn(
                tr,
                include_tool_outputs=include_tool_outputs,
                budget=budget,
            )
            for tr in final_window
        ]
        result: Dict[str, Any] = {
            "threadId": thread_id,
            "waitOutcome": outcome,
            "elapsedSeconds": round(time.monotonic() - start, 2),
            "threadStatus": thread.get("status"),
            "watchedTurnIds": [tr.get("id") for tr in final_window],
            "userInterventions": interventions,
            "turn": formatted_turns[-1] if formatted_turns else None,
        }
        if len(formatted_turns) > 1:
            result["turns"] = formatted_turns
        if budget.exhausted:
            result["outputBudgetExhausted"] = True
        pending = list(getattr(client, "pending_server_requests", ()))
        if pending:
            result["pendingServerRequests"] = pending
        if extra:
            result.update(extra)
        return result

    while True:
        _check_cancelled()
        meta = client.call(
            "thread/read",
            {"threadId": thread_id, "includeTurns": False},
            **_call_rem(),
        )
        thread = (meta or {}).get("thread") or {}
        flags = _active_flags(thread)
        status_obj = thread.get("status")
        thread_active = isinstance(status_obj, dict) and status_obj.get("type") == "active"

        turns_desc, _ = _fetch_turns_until_anchor(
            client,
            thread_id,
            anchor_turn_id,
            items_view="full" if return_on_user_steer else "notLoaded",
            page_limit=20,
            max_pages=10,
            deadline=deadline,
        )
        if anchor_turn_id is None and turns_desc:
            anchor_turn_id = turns_desc[0].get("id")
        window = _slice_turn_window(turns_desc, anchor_turn_id)

        # When follow_new_turns=False, check the target anchor turn's terminal status
        # BEFORE thread-level activeFlags (which may belong to a later turn B).
        if not follow_new_turns and window:
            anchor_turn = window[0]
            if anchor_turn.get("status") in TERMINAL_TURN_STATUSES:
                hydrated = _hydrate_turns(1, anchor_turn_id, only_anchor=True)
                actual_status = (
                    str(hydrated[0].get("status"))
                    if hydrated and hydrated[0].get("status")
                    else str(anchor_turn.get("status"))
                )
                return _build_result(actual_status, thread, hydrated)

        if return_on_user_steer and window:
            anchor_turn = window[0]
            anchor_um_ids = _user_message_ids_in_turn(anchor_turn)
            if baseline_user_msg_ids is None:
                # Only establish baseline once the initial userMessage is present (or
                # the turn is already terminal), avoiding false positives on an empty first poll.
                if anchor_um_ids or anchor_turn.get("status") in TERMINAL_TURN_STATUSES:
                    baseline_user_msg_ids = set(anchor_um_ids)
            else:
                new_anchor_msgs = [
                    it
                    for idx, it in enumerate(anchor_turn.get("items") or [])
                    if isinstance(it, dict)
                    and it.get("type") == "userMessage"
                    and str(it.get("id") or f"idx:{idx}") not in baseline_user_msg_ids
                    and not _is_own_client_message_id(_user_message_client_id(it))
                ]
                has_external_followup = len(window) > 1 and any(
                    not _is_own_client_message_id(_user_message_client_id(it))
                    for tr in window[1:]
                    for it in (tr.get("items") or [])
                    if isinstance(it, dict) and it.get("type") == "userMessage"
                )
                if new_anchor_msgs or has_external_followup:
                    hydrated = _hydrate_turns(
                        len(window), anchor_turn_id, only_anchor=not follow_new_turns
                    )
                    return _build_result(
                        "user_steered",
                        thread,
                        hydrated,
                        extra={
                            "message": (
                                "A new mid-turn steer or follow-up message appeared on this thread. "
                                "Inspect `userInterventions` and continue waiting if appropriate."
                            )
                        },
                    )

        if return_on_user_wait and flags:
            hydrated = _hydrate_turns(
                len(window), anchor_turn_id, only_anchor=not follow_new_turns
            )
            return _build_result(
                "waiting_for_user",
                thread,
                hydrated,
                extra={
                    "activeFlags": flags,
                    "message": (
                        "Codex is waiting for human input/approval in the Codex UI. "
                        "Ask the user to approve or respond in Codex, then call codex_wait_turn again."
                    ),
                },
            )

        if follow_new_turns and window:
            latest_turn = window[-1]
            any_in_progress = any(tr.get("status") == "inProgress" for tr in window)
            if not any_in_progress and latest_turn.get("status") in TERMINAL_TURN_STATUSES:
                should_finish = not thread_active
                if not should_finish:
                    now = time.monotonic()
                    if active_idle_grace_start is None:
                        active_idle_grace_start = now
                    elif now - active_idle_grace_start >= 5.0:
                        should_finish = True
                if should_finish:
                    hydrated = _hydrate_turns(len(window), anchor_turn_id, only_anchor=False)
                    # Re-check hydrated turns to eliminate the race where turn B starts
                    # between the status poll and the final full hydration call.
                    if hydrated and any(tr.get("status") == "inProgress" for tr in hydrated):
                        active_idle_grace_start = None
                        if time.monotonic() >= deadline:
                            return _build_result("timeout", thread, hydrated)
                        _sleep_cancellable(
                            min(poll_interval_seconds, max(0.1, deadline - time.monotonic()))
                        )
                        continue
                    final_status = (
                        str(hydrated[-1].get("status"))
                        if hydrated and hydrated[-1].get("status") in TERMINAL_TURN_STATUSES
                        else str(latest_turn.get("status"))
                    )
                    return _build_result(final_status, thread, hydrated)
            else:
                active_idle_grace_start = None

        if time.monotonic() >= deadline:
            hydrated = _hydrate_turns(
                len(window), anchor_turn_id, only_anchor=not follow_new_turns
            )
            return _build_result("timeout", thread, hydrated)

        _sleep_cancellable(
            min(poll_interval_seconds, max(0.1, deadline - time.monotonic()))
        )


# ---------------------------------------------------------------------------
# Tool implementations (shared by both MCP server and CLI)
# ---------------------------------------------------------------------------


def op_status(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    with _open_client(conn_kw) as client:
        loaded_ids = _list_all_loaded_thread_ids(client)
        return {
            "connected": True,
            "handshake": client.handshake_status,
            "server": client.server_info,
            "loadedThreadCount": len(loaded_ids),
            "loadedThreadIds": loaded_ids,
        }


def op_list_threads(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    limit = max(1, min(int(args.get("limit") or 15), 100))
    sort_key = args.get("sort_key") or "updated_at"
    loaded_only = bool(args.get("loaded_only", False))
    archived_only = bool(
        args.get("archived_only")
        if "archived_only" in args
        else args.get("include_archived", False)
    )
    use_state_db_only = bool(args.get("use_state_db_only", True))

    base_params: Dict[str, Any] = {
        "limit": limit,
        "sortKey": sort_key,
        "archived": archived_only,
    }
    if use_state_db_only:
        base_params["useStateDbOnly"] = True
    if args.get("cwd"):
        base_params["cwd"] = args["cwd"]
    if args.get("search_term"):
        base_params["searchTerm"] = args["search_term"]

    with _open_client(conn_kw) as client:
        loaded_ids = set(_list_all_loaded_thread_ids(client))
        if loaded_only:
            if not loaded_ids:
                return {"threads": [], "loadedCount": 0, "nextCursor": None}
            # Codex rejects includedThreadIds > 100, and older <=0.157 servers ignore it.
            # Send includedThreadIds only when <=100, and always enforce client-side filtering.
            if len(loaded_ids) <= MAX_LOADED_FILTER_IDS:
                base_params["includedThreadIds"] = sorted(loaded_ids)

            collected: List[Dict[str, Any]] = []
            cursor = args.get("cursor")
            next_cursor: Optional[str] = None
            for _ in range(5):
                params = dict(base_params)
                if cursor:
                    params["cursor"] = cursor
                res = client.call("thread/list", params) or {}
                raw_threads = res.get("data") or []
                next_cursor = res.get("nextCursor")
                for t in raw_threads:
                    if isinstance(t, dict) and t.get("id") in loaded_ids:
                        collected.append(t)
                        if len(collected) >= limit:
                            break
                if len(collected) >= limit or not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
            return {
                "loadedCount": len(loaded_ids),
                "threads": [
                    format_thread_summary(t, loaded_ids=loaded_ids) for t in collected
                ],
                "nextCursor": next_cursor,
            }

        params = dict(base_params)
        if args.get("cursor"):
            params["cursor"] = args["cursor"]
        res = client.call("thread/list", params) or {}
        raw_threads = res.get("data") or []
        return {
            "loadedCount": len(loaded_ids),
            "threads": [
                format_thread_summary(t, loaded_ids=loaded_ids) for t in raw_threads
            ],
            "nextCursor": res.get("nextCursor"),
        }


def op_read_thread(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    thread_id = str(args["thread_id"])
    recent_turns = max(0, min(int(args.get("recent_turns", 1)), 20))
    items_view = str(args.get("items_view") or "compact")
    include_tool_outputs = bool(args.get("include_tool_outputs", False))
    max_output_chars = max(500, min(int(args.get("max_output_chars") or 8000), 50000))
    rpc_items_view = _resolve_rpc_items_view(items_view, include_tool_outputs)

    with _open_client(conn_kw) as client:
        loaded_ids = set(_list_all_loaded_thread_ids(client))
        meta = client.call("thread/read", {"threadId": thread_id, "includeTurns": False})
        thread = (meta or {}).get("thread") or {}
        out: Dict[str, Any] = {
            "thread": format_thread_summary(
                thread, loaded_ids=loaded_ids, preview_chars=400
            ),
            "activeFlags": _active_flags(thread),
        }
        if recent_turns > 0:
            turns_res = client.call(
                "thread/turns/list",
                {
                    "threadId": thread_id,
                    "limit": recent_turns,
                    "sortDirection": "desc",
                    "itemsView": rpc_items_view,
                },
            ) or {}
            turns = turns_res.get("data") or turns_res.get("turns") or []
            budget = _OutputBudget()
            out["recentTurns"] = [
                format_turn(
                    tr,
                    include_tool_outputs=include_tool_outputs,
                    max_output_chars=max_output_chars,
                    budget=budget,
                )
                for tr in turns
            ]
            out["nextCursor"] = turns_res.get("nextCursor")
            if budget.exhausted:
                out["outputBudgetExhausted"] = True
        return out


def op_list_turns(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    thread_id = str(args["thread_id"])
    limit = max(1, min(int(args.get("limit") or 5), 50))
    sort_direction = str(args.get("sort_direction") or "desc")
    items_view = str(args.get("items_view") or "compact")
    include_tool_outputs = bool(args.get("include_tool_outputs", False))
    max_output_chars = max(500, min(int(args.get("max_output_chars") or 8000), 50000))
    rpc_items_view = _resolve_rpc_items_view(items_view, include_tool_outputs)

    params: Dict[str, Any] = {
        "threadId": thread_id,
        "limit": limit,
        "sortDirection": sort_direction,
        "itemsView": rpc_items_view,
    }
    if args.get("cursor"):
        params["cursor"] = args["cursor"]

    with _open_client(conn_kw) as client:
        res = client.call("thread/turns/list", params) or {}
        turns = res.get("data") or res.get("turns") or []
        budget = _OutputBudget()
        out: Dict[str, Any] = {
            "threadId": thread_id,
            "count": len(turns),
            "turns": [
                format_turn(
                    tr,
                    include_tool_outputs=include_tool_outputs,
                    max_output_chars=max_output_chars,
                    budget=budget,
                )
                for tr in turns
            ],
            "nextCursor": res.get("nextCursor"),
            "backwardsCursor": res.get("backwardsCursor"),
        }
        if budget.exhausted:
            out["outputBudgetExhausted"] = True
        return out


def op_get_turn_details(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    thread_id = str(args["thread_id"])
    turn_id = str(args["turn_id"])
    max_output_chars = max(500, min(int(args.get("max_output_chars") or 8000), 50000))
    limit = max(1, min(int(args.get("limit") or 100), 100))
    cursor = args.get("cursor")

    params: Dict[str, Any] = {
        "threadId": thread_id,
        "turnId": turn_id,
        "limit": limit,
        "sortDirection": "asc",
    }
    if cursor:
        params["cursor"] = cursor

    with _open_client(conn_kw) as client:
        raw_items: List[Dict[str, Any]] = []
        next_cursor: Optional[str] = None
        fallback_used = False
        fallback_has_more = False
        original_rpc_error: Optional[CodexRpcError] = None

        try:
            res = client.call("thread/items/list", params) or {}
            entries = res.get("data") or []
            raw_items = [
                _unwrap_item_entry(it) for it in entries if isinstance(it, dict)
            ]
            next_cursor = res.get("nextCursor")
        except CodexRpcError as exc:
            # Never swallow cursor errors (-32602), internal errors (-32603), or arbitrary errors.
            # Only attempt turns/list fallback on the first page when items/list is unsupported
            # on an older daemon or when an active non-ephemeral turn has no rollout yet.
            if cursor is not None or not (
                _is_method_not_found_error(exc) or _is_unmaterialized_thread_error(exc)
            ):
                raise
            original_rpc_error = exc
            raw_items = []

        if not raw_items and not cursor:
            try:
                turns_desc, found = _fetch_turns_until_anchor(
                    client,
                    thread_id,
                    turn_id,
                    items_view="full",
                    page_limit=20,
                    max_pages=10,
                )
            except CodexRpcError:
                if original_rpc_error is not None:
                    raise original_rpc_error
                raise
            if found:
                for tr in turns_desc:
                    if isinstance(tr, dict) and tr.get("id") == turn_id:
                        all_turn_items = [
                            _unwrap_item_entry(it)
                            for it in (tr.get("items") or [])
                            if isinstance(it, dict)
                        ]
                        if len(all_turn_items) > limit:
                            fallback_has_more = True
                        raw_items = all_turn_items[:limit]
                        fallback_used = True
                        break
            elif original_rpc_error is not None:
                raise original_rpc_error

        budget = _OutputBudget()
        formatted = []
        for it in raw_items:
            fmt = format_item(
                it,
                include_tool_outputs=True,
                max_output_chars=max_output_chars,
                budget=budget,
            )
            if fmt is not None:
                formatted.append(fmt)
        out: Dict[str, Any] = {
            "threadId": thread_id,
            "turnId": turn_id,
            "count": len(formatted),
            "items": formatted,
            "nextCursor": next_cursor,
        }
        if fallback_used:
            out["source"] = "turns_list_fallback"
            if fallback_has_more:
                out["truncatedByLimit"] = True
        if budget.exhausted:
            out["outputBudgetExhausted"] = True
        return out


def op_wait_turn(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    thread_id = str(args["thread_id"])
    timeout_seconds = max(
        1.0,
        min(
            float(args.get("timeout_seconds", DEFAULT_WAIT_TIMEOUT_SECONDS)),
            MAX_WAIT_TIMEOUT_SECONDS,
        ),
    )
    deadline = time.monotonic() + timeout_seconds
    connect_timeout = min(10.0, max(1.0, timeout_seconds))
    with _open_client(conn_kw, connect_timeout=connect_timeout) as client:
        return _wait_for_turn_readonly(
            client,
            thread_id,
            turn_id=args.get("turn_id"),
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=float(args.get("poll_interval_seconds", 1.5)),
            return_on_user_wait=bool(args.get("return_on_user_wait", True)),
            follow_new_turns=bool(args.get("follow_new_turns", True)),
            return_on_user_steer=bool(args.get("return_on_user_steer", False)),
            include_tool_outputs=bool(args.get("include_tool_outputs", False)),
            deadline_override=deadline,
        )


def op_list_models(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    with _open_client(conn_kw) as client:
        all_models: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(50):
            params: Dict[str, Any] = {}
            if cursor is not None:
                params["cursor"] = cursor
            res = client.call("model/list", params) or {}
            models = res.get("data") or res.get("models") or []
            for m in models:
                if isinstance(m, dict):
                    all_models.append(
                        {
                            k: m.get(k)
                            for k in (
                                "id",
                                "model",
                                "displayName",
                                "description",
                                "isDefault",
                                "defaultReasoningEffort",
                                "supportedReasoningEfforts",
                                "defaultServiceTier",
                                "serviceTiers",
                                "additionalSpeedTiers",
                            )
                            if m.get(k) is not None
                        }
                    )
            next_cursor = res.get("nextCursor")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return {"count": len(all_models), "models": all_models}


def op_start_thread(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    cwd = _resolve_cwd_arg(str(args["cwd"]), conn_kw)
    ephemeral = bool(args.get("ephemeral", False))

    if ephemeral and args.get("name"):
        raise CodexBridgeError(
            "Invalid option combination: `ephemeral=true` threads do not support persistent naming (`thread/name/set`) in Codex. Omit `name` or set `ephemeral=false`."
        )
    if ephemeral and args.get("wait"):
        raise CodexBridgeError(
            "Invalid option combination: `ephemeral=true` threads do not support history-based waiting (`thread/turns/list`). Omit `wait=true` or set `ephemeral=false`."
        )

    start_params: Dict[str, Any] = {"cwd": cwd}
    if args.get("sandbox"):
        start_params["sandbox"] = str(args["sandbox"])
    if args.get("approval_policy"):
        start_params["approvalPolicy"] = str(args["approval_policy"])
    if args.get("approvals_reviewer"):
        start_params["approvalsReviewer"] = str(args["approvals_reviewer"])
    if args.get("model"):
        start_params["model"] = str(args["model"])
    service_tier = _normalize_service_tier(args.get("service_tier"))
    if service_tier:
        start_params["serviceTier"] = service_tier
    if args.get("effort"):
        start_params["config"] = {"model_reasoning_effort": str(args["effort"])}
    if args.get("ephemeral") is not None:
        start_params["ephemeral"] = ephemeral

    wait_requested = bool(args.get("wait"))
    wait_timeout = max(
        1.0,
        min(
            float(args.get("timeout_seconds", DEFAULT_WAIT_TIMEOUT_SECONDS)),
            MAX_WAIT_TIMEOUT_SECONDS,
        ),
    )
    total_deadline = time.monotonic() + wait_timeout if wait_requested else None

    with _open_client(conn_kw) as client:
        res = client.call("thread/start", start_params) or {}
        thread = res.get("thread") or {}
        thread_id = thread.get("id")
        if not thread_id:
            raise CodexBridgeError(f"thread/start did not return a thread id: {res}")

        out: Dict[str, Any] = {
            "threadId": thread_id,
            "name": thread.get("name"),
            "cwd": res.get("cwd") or cwd,
            "model": res.get("model"),
            "reasoningEffort": res.get("reasoningEffort"),
            "serviceTier": res.get("serviceTier"),
            "sandbox": res.get("sandbox"),
            "approvalPolicy": res.get("approvalPolicy"),
            "approvalsReviewer": res.get("approvalsReviewer"),
        }
        if ephemeral:
            out["ephemeral"] = True
            out["resumable"] = False
        else:
            out["resumeCommand"] = _build_resume_command(thread_id, conn_kw)

        name = args.get("name")
        if name:
            try:
                client.call("thread/name/set", {"threadId": thread_id, "name": str(name)})
                out["name"] = str(name)
            except Exception as exc:
                raise CodexPartialSuccessError(
                    f"Thread {thread_id} was created, but setting name failed: {exc}",
                    {**out, "partialSuccess": True, "failedStage": "thread/name/set", "error": str(exc)},
                ) from exc

        prompt = args.get("prompt")
        if prompt:
            turn_params: Dict[str, Any] = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": str(prompt)}],
            }
            if args.get("effort"):
                turn_params["effort"] = str(args["effort"])
            try:
                turn_res = client.call("turn/start", turn_params) or {}
            except Exception as exc:
                raise CodexPartialSuccessError(
                    f"Thread {thread_id} was created, but turn/start failed: {exc}",
                    {**out, "partialSuccess": True, "failedStage": "turn/start", "error": str(exc)},
                ) from exc
            turn = turn_res.get("turn") or {}
            out["turnId"] = turn.get("id")
            out["turnStatus"] = turn.get("status")
            if wait_requested:
                try:
                    out["waitResult"] = _wait_for_turn_readonly(
                        client,
                        thread_id,
                        turn_id=turn.get("id"),
                        timeout_seconds=wait_timeout,
                        include_tool_outputs=bool(args.get("include_tool_outputs", False)),
                        deadline_override=total_deadline,
                    )
                except Exception as exc:
                    raise CodexPartialSuccessError(
                        f"Thread {thread_id} turn {out.get('turnId')} started, but waiting failed: {exc}",
                        {**out, "partialSuccess": True, "failedStage": "wait", "error": str(exc)},
                    ) from exc
        return out


def op_fork_thread(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    thread_id = str(args["thread_id"])
    fork_params: Dict[str, Any] = {
        "threadId": thread_id,
        "excludeTurns": True,
    }
    if args.get("cwd"):
        fork_params["cwd"] = _resolve_cwd_arg(str(args["cwd"]), conn_kw)
    if args.get("sandbox"):
        fork_params["sandbox"] = str(args["sandbox"])
    if args.get("approval_policy"):
        fork_params["approvalPolicy"] = str(args["approval_policy"])
    if args.get("approvals_reviewer"):
        fork_params["approvalsReviewer"] = str(args["approvals_reviewer"])
    service_tier = _normalize_service_tier(args.get("service_tier"))
    if service_tier:
        fork_params["serviceTier"] = service_tier

    with _open_client(conn_kw) as client:
        res = client.call("thread/fork", fork_params) or {}
        thread = res.get("thread") or {}
        new_id = thread.get("id")
        if not new_id:
            raise CodexBridgeError(f"thread/fork did not return a new thread id: {res}")
        out: Dict[str, Any] = {
            "forkedFromId": thread_id,
            "threadId": new_id,
            "name": thread.get("name"),
            "cwd": res.get("cwd") or thread.get("cwd"),
            "model": res.get("model") or thread.get("model"),
            "serviceTier": res.get("serviceTier"),
            "resumeCommand": _build_resume_command(new_id, conn_kw),
        }
        if args.get("name"):
            try:
                client.call("thread/name/set", {"threadId": new_id, "name": str(args["name"])})
                out["name"] = str(args["name"])
            except Exception as exc:
                raise CodexPartialSuccessError(
                    f"Forked thread {new_id} was created, but setting name failed: {exc}",
                    {**out, "partialSuccess": True, "failedStage": "thread/name/set", "error": str(exc)},
                ) from exc
        return out


def op_send_turn(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    _require_modify_confirmation(args, "codex_send_turn")
    thread_id = str(args["thread_id"])
    prompt = str(args["prompt"])

    wait_requested = bool(args.get("wait"))
    wait_timeout = max(
        1.0,
        min(
            float(args.get("timeout_seconds", DEFAULT_WAIT_TIMEOUT_SECONDS)),
            MAX_WAIT_TIMEOUT_SECONDS,
        ),
    )
    total_deadline = time.monotonic() + wait_timeout if wait_requested else None

    with _open_client(conn_kw) as client:
        thread = _ensure_loaded(client, thread_id)
        status_obj = thread.get("status")
        thread_active = isinstance(status_obj, dict) and status_obj.get("type") == "active"

        latest: Optional[Dict[str, Any]] = None
        try:
            latest_res = client.call(
                "thread/turns/list",
                {
                    "threadId": thread_id,
                    "limit": 1,
                    "sortDirection": "desc",
                    "itemsView": "notLoaded",
                },
            ) or {}
            latest = _extract_latest_turn(latest_res)
        except CodexRpcError as exc:
            # Brand-new threads with no rollout/turns yet can legitimately fail `thread/turns/list`
            # before their first message; allow proceeding if the thread is not active.
            if _is_unmaterialized_thread_error(exc) and not thread_active:
                latest = None
            else:
                raise

        if latest and latest.get("status") == "inProgress":
            raise CodexBridgeError(
                f"Thread {thread_id} currently has an inProgress turn ({latest.get('id')}). "
                "Use `codex_queue_message` to queue a follow-up, `codex_steer_turn` to steer "
                "the active turn, or `codex_wait_turn` to wait for completion first."
            )

        turn_params: Dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
        }
        if args.get("model"):
            turn_params["model"] = str(args["model"])
        if args.get("effort"):
            turn_params["effort"] = str(args["effort"])
        service_tier = _normalize_service_tier(args.get("service_tier"))
        if service_tier:
            turn_params["serviceTier"] = service_tier
        service_tier_for_turn = _normalize_service_tier(args.get("service_tier_for_turn"))
        if service_tier_for_turn:
            turn_params["serviceTierForTurn"] = service_tier_for_turn
        if args.get("approval_policy"):
            turn_params["approvalPolicy"] = str(args["approval_policy"])

        turn_res = client.call("turn/start", turn_params) or {}
        turn = turn_res.get("turn") or {}
        out: Dict[str, Any] = {
            "threadId": thread_id,
            "turnId": turn.get("id"),
            "turnStatus": turn.get("status"),
        }
        if wait_requested:
            try:
                out["waitResult"] = _wait_for_turn_readonly(
                    client,
                    thread_id,
                    turn_id=turn.get("id"),
                    timeout_seconds=wait_timeout,
                    include_tool_outputs=bool(args.get("include_tool_outputs", False)),
                    deadline_override=total_deadline,
                )
            except Exception as exc:
                raise CodexPartialSuccessError(
                    f"Turn {out.get('turnId')} started on {thread_id}, but waiting failed: {exc}",
                    {**out, "partialSuccess": True, "failedStage": "wait", "error": str(exc)},
                ) from exc
        return out


def op_steer_turn(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    _require_modify_confirmation(args, "codex_steer_turn")
    thread_id = str(args["thread_id"])
    prompt = str(args["prompt"])

    with _open_client(conn_kw) as client:
        _ensure_loaded(client, thread_id)
        expected_turn_id = args.get("expected_turn_id")
        if not expected_turn_id:
            latest_res = client.call(
                "thread/turns/list",
                {
                    "threadId": thread_id,
                    "limit": 1,
                    "sortDirection": "desc",
                    "itemsView": "notLoaded",
                },
            ) or {}
            latest = _extract_latest_turn(latest_res)
            if not latest or latest.get("status") != "inProgress":
                raise CodexBridgeError(
                    f"Thread {thread_id} has no inProgress turn to steer. "
                    "Use `codex_send_turn` to start a new turn instead."
                )
            expected_turn_id = latest.get("id")

        res = client.call(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": str(expected_turn_id),
                "input": [{"type": "text", "text": prompt}],
            },
        )
        return {
            "threadId": thread_id,
            "steeredTurnId": str(expected_turn_id),
            "result": res,
        }


def op_interrupt_turn(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    _require_modify_confirmation(args, "codex_interrupt_turn")
    thread_id = str(args["thread_id"])

    with _open_client(conn_kw) as client:
        _ensure_loaded(client, thread_id)
        turn_id = args.get("turn_id")
        if not turn_id:
            latest_res = client.call(
                "thread/turns/list",
                {
                    "threadId": thread_id,
                    "limit": 1,
                    "sortDirection": "desc",
                    "itemsView": "notLoaded",
                },
            ) or {}
            latest = _extract_latest_turn(latest_res)
            if not latest or latest.get("status") != "inProgress":
                return {
                    "threadId": thread_id,
                    "interruptAcknowledged": False,
                    "interrupted": False,
                    "message": "No inProgress turn found on this thread.",
                }
            turn_id = latest.get("id")

        client.call("turn/interrupt", {"threadId": thread_id, "turnId": str(turn_id)})

        # Codex acknowledges `turn/interrupt` with `{}` both when the turn aborted
        # and when it completed naturally just before the interrupt landed. Inspect
        # the resulting turn status best-effort to report accurately.
        actual_status: Optional[str] = None
        try:
            check_res = client.call(
                "thread/turns/list",
                {
                    "threadId": thread_id,
                    "limit": 5,
                    "sortDirection": "desc",
                    "itemsView": "notLoaded",
                },
            ) or {}
            for tr in check_res.get("data") or check_res.get("turns") or []:
                if isinstance(tr, dict) and tr.get("id") == str(turn_id):
                    actual_status = tr.get("status")
                    break
        except Exception:
            actual_status = None

        out: Dict[str, Any] = {
            "threadId": thread_id,
            "turnId": str(turn_id),
            "interruptAcknowledged": True,
        }
        if actual_status is not None:
            out["turnStatus"] = actual_status
            out["interrupted"] = actual_status == "interrupted"
        else:
            out["interrupted"] = None
        return out


def op_queue_message(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    _require_modify_confirmation(args, "codex_queue_message")
    thread_id = str(args["thread_id"])
    message = str(args["message"])
    resume = bool(args.get("resume", False))
    client_msg_id = str(uuid.uuid4())

    with _open_client(conn_kw) as client:
        try:
            rpc_res = client.call(
                "thread/queue/add",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": message}],
                    "clientUserMessageId": client_msg_id,
                },
            ) or {}
        except CodexRpcError as exc:
            if _is_method_not_found_error(exc):
                raise CodexBridgeError(
                    "The connected Codex app-server daemon does not support `thread/queue/add`. "
                    "Upgrade Codex CLI or wait for the thread to settle and use `codex_send_turn`."
                ) from exc
            raise

        _record_own_client_message_id(client_msg_id)
        out: Dict[str, Any] = {
            "threadId": thread_id,
            "queued": True,
            "via": "app-server-rpc",
            "clientUserMessageId": client_msg_id,
            "queuedSubmission": rpc_res.get("queuedSubmission"),
        }

        # Enqueue already succeeded; never re-enqueue if post-enqueue resume fails.
        if resume:
            try:
                _ensure_loaded(client, thread_id)
                out["resumed"] = True
            except Exception as exc:
                out["resumed"] = False
                out["warning"] = f"Message was queued, but resuming thread failed: {exc}"
        return out


def op_start_review(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    target_type = str(args.get("target_type") or "uncommittedChanges")
    if target_type == "uncommittedChanges":
        target: Dict[str, Any] = {"type": "uncommittedChanges"}
        label = "uncommitted changes"
    elif target_type == "baseBranch":
        branch = str(args.get("branch") or "")
        if not branch:
            raise CodexBridgeError("`branch` is required when target_type='baseBranch'")
        target = {"type": "baseBranch", "branch": branch}
        label = f"vs {branch}"
    elif target_type == "commit":
        sha = str(args.get("sha") or "")
        if not sha:
            raise CodexBridgeError("`sha` is required when target_type='commit'")
        target = {"type": "commit", "sha": sha}
        label = sha[:10]
    else:
        raise CodexBridgeError(
            f"Unsupported target_type {target_type!r}; use uncommittedChanges, baseBranch, or commit."
        )

    thread_id = args.get("thread_id")
    if thread_id:
        _require_modify_confirmation(args, "codex_start_review (on existing thread)")
        if args.get("confirm_replace_active_turn") is not True:
            raise CodexBridgeError(
                "Safety guardrail: running `codex_start_review` inline on an existing thread "
                "aborts and replaces any active turn on that thread (`TurnAbortReason::Replaced`). "
                "Omit `thread_id` (or call `codex_fork_thread` first) to review safely in an isolated thread, "
                "or explicitly pass `confirm_replace_active_turn=true` if you intend to replace any running task."
            )

    wait_requested = bool(args.get("wait"))
    wait_timeout = max(
        1.0,
        min(
            float(args.get("timeout_seconds", DEFAULT_WAIT_TIMEOUT_SECONDS)),
            MAX_WAIT_TIMEOUT_SECONDS,
        ),
    )
    total_deadline = time.monotonic() + wait_timeout if wait_requested else None

    created_new = False
    started_meta: Dict[str, Any] = {}
    with _open_client(conn_kw) as client:
        if thread_id:
            thread_id = str(thread_id)
            _ensure_loaded(client, thread_id)
        else:
            cwd_arg = args.get("cwd")
            if not cwd_arg:
                raise CodexBridgeError("`cwd` is required when `thread_id` is omitted")
            cwd = _resolve_cwd_arg(str(cwd_arg), conn_kw)
            start_params: Dict[str, Any] = {
                "cwd": cwd,
                "sandbox": "read-only",
            }
            if args.get("model"):
                start_params["model"] = str(args["model"])
            service_tier = _normalize_service_tier(args.get("service_tier"))
            if service_tier:
                start_params["serviceTier"] = service_tier
            if args.get("effort"):
                start_params["config"] = {"model_reasoning_effort": str(args["effort"])}
            started_meta = client.call("thread/start", start_params) or {}
            thread_id = (started_meta.get("thread") or {}).get("id")
            if not thread_id:
                raise CodexBridgeError("Failed to create review thread")
            created_new = True
            title = str(args.get("name") or f"AGY Review ({label})")
            try:
                client.call("thread/name/set", {"threadId": thread_id, "name": title})
            except Exception as exc:
                raise CodexPartialSuccessError(
                    f"Review thread {thread_id} was created, but setting name failed: {exc}",
                    {
                        "threadId": thread_id,
                        "createdNewThread": True,
                        "resumeCommand": _build_resume_command(thread_id, conn_kw),
                        "partialSuccess": True,
                        "failedStage": "thread/name/set",
                        "error": str(exc),
                    },
                ) from exc

        try:
            res = client.call(
                "review/start",
                {"threadId": thread_id, "target": target, "delivery": "inline"},
            ) or {}
        except Exception as exc:
            if created_new:
                raise CodexPartialSuccessError(
                    f"Review thread {thread_id} was created, but review/start failed: {exc}",
                    {
                        "threadId": thread_id,
                        "createdNewThread": True,
                        "resumeCommand": _build_resume_command(thread_id, conn_kw),
                        "partialSuccess": True,
                        "failedStage": "review/start",
                        "error": str(exc),
                    },
                ) from exc
            raise

        turn = res.get("turn") or {}
        review_thread_id = str(res.get("reviewThreadId") or thread_id)
        out: Dict[str, Any] = {
            "threadId": review_thread_id,
            "createdNewThread": created_new,
            "target": target,
            "turnId": turn.get("id"),
            "turnStatus": turn.get("status"),
            "resumeCommand": _build_resume_command(review_thread_id, conn_kw),
        }
        if created_new:
            out["threadConfig"] = {
                "model": started_meta.get("model"),
                "reasoningEffort": started_meta.get("reasoningEffort"),
                "serviceTier": started_meta.get("serviceTier"),
            }
            out["reviewerModelNote"] = (
                "Values under `threadConfig` reflect the created parent thread; Codex native review "
                "may use its configured `review_model` and adjust effort/serviceTier independently."
            )
        if wait_requested:
            try:
                out["waitResult"] = _wait_for_turn_readonly(
                    client,
                    review_thread_id,
                    turn_id=turn.get("id"),
                    timeout_seconds=wait_timeout,
                    include_tool_outputs=bool(args.get("include_tool_outputs", False)),
                    deadline_override=total_deadline,
                )
            except Exception as exc:
                raise CodexPartialSuccessError(
                    f"Review turn {out.get('turnId')} started on {review_thread_id}, but waiting failed: {exc}",
                    {**out, "partialSuccess": True, "failedStage": "wait", "error": str(exc)},
                ) from exc
        return out


# ---------------------------------------------------------------------------
# MCP Tool Registry
# ---------------------------------------------------------------------------


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "codex_status",
        "description": "Check connectivity to the Codex app-server daemon and list currently loaded thread IDs. Does not modify thread conversations.",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": op_status,
    },
    {
        "name": "codex_list_threads",
        "description": "List Codex threads from the shared app-server daemon with compact metadata (id, name, status, isLoaded, model, cwd, updatedAt, short preview). Uses indexed state DB metadata by default (use_state_db_only=true) to avoid rollout repair writes.",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 15},
                "cwd": {"type": "string", "description": "Optional exact working directory filter."},
                "search_term": {"type": "string", "description": "Optional search keyword."},
                "loaded_only": {"type": "boolean", "default": False, "description": "Only return threads currently loaded in daemon memory."},
                "archived_only": {"type": "boolean", "default": False, "description": "When true, return ONLY archived threads; when false, return ONLY non-archived threads."},
                "use_state_db_only": {"type": "boolean", "default": True, "description": "Query indexed state DB without scanning JSONL rollouts for metadata repairs."},
                "sort_key": {"type": "string", "enum": ["updated_at", "created_at", "recency_at"], "default": "updated_at"},
                "cursor": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "handler": op_list_threads,
    },
    {
        "name": "codex_read_thread",
        "description": "Read metadata, live status/activeFlags (including waitingOnApproval/waitingOnUserInput), and the latest N turns of a Codex thread. Defaults to recent_turns=1 and items_view='compact' (preserves ALL userMessages including mid-turn steers and long final answers up to 24k chars while compacting heavy tool outputs).",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {
                "thread_id": {"type": "string"},
                "recent_turns": {"type": "integer", "minimum": 0, "maximum": 20, "default": 1},
                "items_view": {"type": "string", "enum": ["compact", "summary", "full", "notLoaded"], "default": "compact"},
                "include_tool_outputs": {"type": "boolean", "default": False, "description": "Include raw command stdout and file diffs (larger response)."},
                "max_output_chars": {"type": "integer", "minimum": 500, "maximum": 50000, "default": 8000},
            },
            "additionalProperties": False,
        },
        "handler": op_read_thread,
    },
    {
        "name": "codex_list_turns",
        "description": "Paginate turn history of a Codex thread. Defaults to newest-first (desc), limit=5, compact view (preserves all userMessages & mid-turn steers while compacting heavy tool outputs).",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {
                "thread_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5},
                "sort_direction": {"type": "string", "enum": ["desc", "asc"], "default": "desc"},
                "items_view": {"type": "string", "enum": ["compact", "summary", "full", "notLoaded"], "default": "compact"},
                "include_tool_outputs": {"type": "boolean", "default": False},
                "max_output_chars": {"type": "integer", "minimum": 500, "maximum": 50000, "default": 8000},
                "cursor": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "handler": op_list_turns,
    },
    {
        "name": "codex_get_turn_details",
        "description": "Fetch full items for a single Codex turn, including raw command outputs, file diffs, and item timestamps (bounded per item by max_output_chars and overall response budget).",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "turn_id"],
            "properties": {
                "thread_id": {"type": "string"},
                "turn_id": {"type": "string"},
                "max_output_chars": {"type": "integer", "minimum": 500, "maximum": 50000, "default": 8000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
                "cursor": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "handler": op_get_turn_details,
    },
    {
        "name": "codex_wait_turn",
        "description": "Wait (0 LLM polling tokens; supports MCP cancellation) until Codex finishes the active turn (and any follow-up turns when follow_new_turns=true) or enters waitingOnApproval/waitingOnUserInput. Hydrates full turn items on completion so `userInterventions` and `turn.midTurnSteers` expose mid-flight messages.",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {
                "thread_id": {"type": "string"},
                "turn_id": {"type": "string", "description": "Optional starting turn ID; defaults to the latest turn."},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 600, "default": 120},
                "poll_interval_seconds": {"type": "number", "minimum": 0.5, "maximum": 10, "default": 1.5},
                "return_on_user_wait": {"type": "boolean", "default": True},
                "follow_new_turns": {"type": "boolean", "default": True, "description": "Keep waiting if a follow-up turn starts while the thread stays active."},
                "return_on_user_steer": {"type": "boolean", "default": False, "description": "If true, return immediately with waitOutcome='user_steered' when an external mid-turn steer or follow-up message appears."},
                "include_tool_outputs": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "handler": op_wait_turn,
    },
    {
        "name": "codex_list_models",
        "description": "List available Codex models, reasoning efforts, and service tiers (e.g. fast/priority) advertised by the running Codex app-server. May trigger a catalog fetch on cache miss.",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": op_list_models,
    },
    {
        "name": "codex_start_thread",
        "description": "Create a NEW Codex thread on the shared daemon (visible in Codex UIs attached to the same daemon) and optionally send an initial prompt. Safe default for AGY-owned delegated tasks.",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
        "inputSchema": {
            "type": "object",
            "required": ["cwd"],
            "properties": {
                "cwd": {"type": "string", "description": "Absolute working directory for the Codex thread."},
                "name": {"type": "string", "description": "Human-readable title shown in Codex UI sidebar (not supported with ephemeral=true)."},
                "prompt": {"type": "string", "description": "Optional initial prompt to start immediately."},
                "model": {"type": "string"},
                "effort": {"type": "string", "description": "Reasoning effort override (e.g. medium, high, xhigh, max)."},
                "service_tier": {"type": "string", "description": "Optional service tier override (e.g. 'fast'/'priority' for Fast mode, or 'default' for standard); omit to inherit ~/.codex/config.toml default."},
                "sandbox": {"type": "string", "enum": ["read-only", "workspace-write", "danger-full-access"], "description": "Optional sandbox override; omit to inherit ~/.codex/config.toml default."},
                "approval_policy": {"type": "string", "enum": ["on-request", "untrusted", "never"], "description": "Optional approval policy override; omit to inherit ~/.codex/config.toml default."},
                "approvals_reviewer": {"type": "string", "enum": ["user", "auto_review"], "description": "Optional approvals reviewer override; omit to inherit ~/.codex/config.toml default."},
                "ephemeral": {"type": "boolean", "default": False, "description": "Create an in-memory ephemeral thread (incompatible with `name`, `wait=true`, and `codex resume`)."},
                "wait": {"type": "boolean", "default": False, "description": "Wait for the initial turn to finish before returning."},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 600, "default": 120},
                "include_tool_outputs": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "handler": op_start_thread,
    },
    {
        "name": "codex_fork_thread",
        "description": "Fork an existing Codex thread into a NEW isolated thread with the same history. Use this instead of writing directly to a user's active thread when you want its context without disturbing it.",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {
                "thread_id": {"type": "string"},
                "name": {"type": "string"},
                "cwd": {"type": "string"},
                "service_tier": {"type": "string", "description": "Optional service tier override (e.g. 'fast'/'priority' or 'default')."},
                "sandbox": {"type": "string", "enum": ["read-only", "workspace-write", "danger-full-access"]},
                "approval_policy": {"type": "string", "enum": ["on-request", "untrusted", "never"]},
                "approvals_reviewer": {"type": "string", "enum": ["user", "auto_review"]},
            },
            "additionalProperties": False,
        },
        "handler": op_fork_thread,
    },
    {
        "name": "codex_send_turn",
        "description": "Send a prompt to an existing Codex thread. Performs a best-effort idle check first (note: concurrent UI submissions between check and `turn/start` may still be converted to a steer by Codex, where `service_tier_for_turn` does not apply; use `codex_start_thread` or `codex_fork_thread` when strict isolation is required). GUARDRAIL: Requires confirm_modify_thread=true.",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "prompt", "confirm_modify_thread"],
            "properties": {
                "thread_id": {"type": "string"},
                "prompt": {"type": "string"},
                "confirm_modify_thread": {"type": "boolean", "description": "Must be true ONLY if this thread is AGY-owned or the user explicitly authorized modifying it."},
                "model": {"type": "string"},
                "effort": {"type": "string"},
                "service_tier": {"type": "string", "description": "Override the thread's service tier for this and subsequent turns (e.g. 'fast'/'priority' or 'default')."},
                "service_tier_for_turn": {"type": "string", "description": "Override the service tier for THIS turn only when starting a new turn (e.g. 'fast'/'priority' or 'default')."},
                "approval_policy": {"type": "string", "enum": ["on-request", "untrusted", "never"]},
                "wait": {"type": "boolean", "default": False},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 600, "default": 120},
                "include_tool_outputs": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "handler": op_send_turn,
    },
    {
        "name": "codex_steer_turn",
        "description": "Steer an actively running (inProgress) Codex turn mid-flight with a course-correction message (uses server-enforced `expectedTurnId`). GUARDRAIL: Requires confirm_modify_thread=true.",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "prompt", "confirm_modify_thread"],
            "properties": {
                "thread_id": {"type": "string"},
                "prompt": {"type": "string"},
                "confirm_modify_thread": {"type": "boolean"},
                "expected_turn_id": {"type": "string", "description": "Optional active turn ID precondition; auto-resolved if omitted."},
            },
            "additionalProperties": False,
        },
        "handler": op_steer_turn,
    },
    {
        "name": "codex_queue_message",
        "description": "Queue a follow-up message on an existing Codex thread via native `thread/queue/add` RPC without interrupting an ongoing turn. GUARDRAIL: Requires confirm_modify_thread=true.",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "message", "confirm_modify_thread"],
            "properties": {
                "thread_id": {"type": "string"},
                "message": {"type": "string"},
                "confirm_modify_thread": {"type": "boolean"},
                "resume": {"type": "boolean", "default": False, "description": "Ensure the thread is loaded in daemon memory after enqueueing."},
            },
            "additionalProperties": False,
        },
        "handler": op_queue_message,
    },
    {
        "name": "codex_interrupt_turn",
        "description": "Request interruption of a currently running Codex turn and report acknowledgment plus resulting turn status. GUARDRAIL: Requires confirm_modify_thread=true.",
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "confirm_modify_thread"],
            "properties": {
                "thread_id": {"type": "string"},
                "turn_id": {"type": "string"},
                "confirm_modify_thread": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        "handler": op_interrupt_turn,
    },
    {
        "name": "codex_start_review",
        "description": "Start a native Codex code review (uncommittedChanges, baseBranch, or commit). Omit `thread_id` (recommended) to safely create a new isolated read-only review thread. Passing `thread_id` runs inline and aborts any running task on that thread, requiring both `confirm_modify_thread=true` and `confirm_replace_active_turn=true`.",
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "description": "Absolute repo path (required when thread_id is omitted)."},
                "target_type": {"type": "string", "enum": ["uncommittedChanges", "baseBranch", "commit"], "default": "uncommittedChanges"},
                "branch": {"type": "string", "description": "Base branch name (required for target_type='baseBranch')."},
                "sha": {"type": "string", "description": "Commit SHA (required for target_type='commit')."},
                "thread_id": {"type": "string", "description": "Optional existing thread ID; runs inline and replaces any running turn on that thread."},
                "confirm_modify_thread": {"type": "boolean", "default": False},
                "confirm_replace_active_turn": {"type": "boolean", "default": False, "description": "Required together with confirm_modify_thread=true when targeting an existing `thread_id`."},
                "name": {"type": "string"},
                "model": {"type": "string", "description": "Optional parent-thread model override when creating a new review thread."},
                "effort": {"type": "string", "description": "Optional parent-thread reasoning effort override when creating a new review thread."},
                "service_tier": {"type": "string", "description": "Optional parent-thread service tier override when creating a new review thread (e.g. 'fast'/'priority' or 'default')."},
                "wait": {"type": "boolean", "default": False},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 600, "default": 120},
                "include_tool_outputs": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "handler": op_start_review,
    },
]

TOOL_MAP: Dict[str, Dict[str, Any]] = {t["name"]: t for t in TOOLS}


# ---------------------------------------------------------------------------
# Strict MCP inputSchema validation
# ---------------------------------------------------------------------------


def _validate_tool_arguments(schema: Dict[str, Any], arguments: Any) -> Dict[str, Any]:
    if arguments is None:
        args: Dict[str, Any] = {}
    elif isinstance(arguments, dict):
        args = arguments
    else:
        raise McpProtocolError(-32602, "Invalid params: `arguments` must be a JSON object")

    props = schema.get("properties") or {}
    required = schema.get("required") or []

    if schema.get("additionalProperties") is False:
        unknown = [k for k in args if k not in props]
        if unknown:
            raise McpProtocolError(
                -32602, f"Invalid params: unexpected argument(s): {', '.join(sorted(unknown))}"
            )

    for key in required:
        if key not in args or args[key] is None:
            raise McpProtocolError(
                -32602, f"Invalid params: missing required argument `{key}`"
            )

    for key, val in args.items():
        spec = props.get(key)
        if spec is None:
            continue
        if val is None:
            # Optional parameters omitted or null are treated as unset.
            continue
        expected_type = spec.get("type")
        if expected_type == "boolean":
            if not isinstance(val, bool):
                raise McpProtocolError(
                    -32602, f"Invalid params: `{key}` must be a boolean, got {type(val).__name__}"
                )
        elif expected_type == "integer":
            if isinstance(val, bool) or not isinstance(val, int):
                raise McpProtocolError(
                    -32602, f"Invalid params: `{key}` must be an integer, got {type(val).__name__}"
                )
        elif expected_type == "number":
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
                raise McpProtocolError(
                    -32602, f"Invalid params: `{key}` must be a finite number, got {type(val).__name__}"
                )
        elif expected_type == "string":
            if not isinstance(val, str):
                raise McpProtocolError(
                    -32602, f"Invalid params: `{key}` must be a string, got {type(val).__name__}"
                )

        if "enum" in spec and val not in spec["enum"]:
            raise McpProtocolError(
                -32602,
                f"Invalid params: `{key}` must be one of {spec['enum']}, got {val!r}",
            )
        if "minimum" in spec and isinstance(val, (int, float)) and val < spec["minimum"]:
            raise McpProtocolError(
                -32602, f"Invalid params: `{key}` must be >= {spec['minimum']}, got {val}"
            )
        if "maximum" in spec and isinstance(val, (int, float)) and val > spec["maximum"]:
            raise McpProtocolError(
                -32602, f"Invalid params: `{key}` must be <= {spec['maximum']}, got {val}"
            )

    return args


def _jsonrpc_error(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def _valid_request_id(req_id: Any) -> bool:
    return (
        isinstance(req_id, str)
        or (isinstance(req_id, int) and not isinstance(req_id, bool))
    )


# ---------------------------------------------------------------------------
# Pure-stdlib MCP stdio server (JSONL + Content-Length, batching, cancellation)
# ---------------------------------------------------------------------------


class McpStdioServer:
    def __init__(self, conn_kw: Dict[str, Any]):
        self.conn_kw = conn_kw
        self.framing = "jsonl"
        self.negotiated_protocol: Optional[str] = None
        self._write_lock = threading.Lock()
        self._active_lock = threading.Lock()
        self._active_requests: Dict[Any, threading.Event] = {}
        self._workers: List[threading.Thread] = []

    def _read_message(self) -> Tuple[Optional[Any], Optional[str]]:
        """Read next raw MCP message. Returns `(parsed_json, parse_error)`."""
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

    def handle_request(
        self,
        req: Any,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(req, dict):
            return _jsonrpc_error(None, -32600, "Invalid Request: expected JSON object")

        has_id = "id" in req
        req_id = req.get("id")
        if has_id and not _valid_request_id(req_id):
            return _jsonrpc_error(
                None, -32600, "Invalid Request: `id` must be a non-null string or integer"
            )

        method = req.get("method")
        if not isinstance(method, str) or not method:
            if not has_id:
                return None
            return _jsonrpc_error(
                req_id, -32600, "Invalid Request: `method` must be a non-empty string"
            )

        raw_params = req.get("params")
        if raw_params is None:
            params: Dict[str, Any] = {}
        elif isinstance(raw_params, dict):
            params = raw_params
        else:
            if not has_id:
                return None
            return _jsonrpc_error(
                req_id, -32602, "Invalid params: `params` must be a JSON object"
            )

        if not has_id:
            if method == "notifications/cancelled":
                target_id = params.get("requestId")
                if target_id is not None:
                    self.cancel_request(target_id)
            return None

        if method == "initialize":
            client_proto = params.get("protocolVersion")
            proto = (
                client_proto
                if isinstance(client_proto, str) and client_proto in SUPPORTED_MCP_PROTOCOLS
                else SUPPORTED_MCP_PROTOCOLS[0]
            )
            self.negotiated_protocol = proto
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": proto,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                    "instructions": (
                        "Bridge to the OpenAI Codex app-server daemon. "
                        "Read-only tools (codex_status, codex_list_threads, codex_read_thread, "
                        "codex_list_turns, codex_get_turn_details, codex_wait_turn, codex_list_models) "
                        "never submit prompts or modify thread conversations. Never send/steer/queue/"
                        "interrupt/review an existing user thread unless explicitly instructed by the user; "
                        "prefer codex_start_thread or codex_fork_thread for AGY-initiated work."
                    ),
                },
            }

        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        if method == "tools/list":
            listed = [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "inputSchema": t["inputSchema"],
                    "annotations": t["annotations"],
                }
                for t in TOOLS
            ]
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": listed}}

        if method == "resources/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}

        if method == "prompts/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}

        if method == "tools/call":
            tool_name = params.get("name")
            if not isinstance(tool_name, str) or not tool_name:
                return _jsonrpc_error(
                    req_id, -32602, "Invalid params: `name` must be a non-empty string"
                )
            tool = TOOL_MAP.get(tool_name)
            if tool is None:
                return _jsonrpc_error(req_id, -32602, f"Unknown tool: {tool_name}")

            try:
                validated_args = _validate_tool_arguments(
                    tool["inputSchema"], params.get("arguments")
                )
            except McpProtocolError as exc:
                return _jsonrpc_error(req_id, exc.code, exc.message)

            prev_event = getattr(_CANCEL_CTX, "event", None)
            _CANCEL_CTX.event = cancel_event
            try:
                data = tool["handler"](validated_args, self.conn_kw)
                text = json.dumps(data, ensure_ascii=False, indent=2)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": text}],
                        "isError": False,
                    },
                }
            except CodexCancelledError:
                return None
            except CodexPartialSuccessError as exc:
                text = json.dumps(exc.partial_result, ensure_ascii=False, indent=2)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": text}],
                        "isError": True,
                    },
                }
            except Exception as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Error: {exc}"}],
                        "isError": True,
                    },
                }
            finally:
                _CANCEL_CTX.event = prev_event

        return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")

    def handle_message(self, msg: Any) -> Optional[Any]:
        """Handle a parsed JSON-RPC object or batch array."""
        if isinstance(msg, list):
            if not msg:
                return _jsonrpc_error(None, -32600, "Invalid Request: empty JSON-RPC batch")
            if self.negotiated_protocol == "2025-06-18":
                return _jsonrpc_error(
                    None,
                    -32600,
                    "Invalid Request: JSON-RPC batching is not supported in MCP 2025-06-18",
                )
            responses: List[Dict[str, Any]] = []
            for item in msg:
                resp = self.handle_request(item)
                if resp is not None:
                    responses.append(resp)
            return responses if responses else None
        return self.handle_request(msg)

    def _dispatch_tool_call_async(self, req: Dict[str, Any]) -> None:
        req_id = req["id"]
        cancel_event = threading.Event()
        with self._active_lock:
            self._active_requests[req_id] = cancel_event

        def _worker() -> None:
            try:
                resp = self.handle_request(req, cancel_event=cancel_event)
                if resp is not None and not cancel_event.is_set():
                    self._write_message(resp)
            finally:
                with self._active_lock:
                    self._active_requests.pop(req_id, None)

        thread = threading.Thread(
            target=_worker, name=f"mcp-tool-{req_id}", daemon=True
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
                resp = self.handle_message(msg)
                if resp is not None:
                    self._write_message(resp)
        finally:
            for t in list(self._workers):
                t.join(timeout=2.0)
            with self._active_lock:
                for ev in self._active_requests.values():
                    ev.set()
            for t in list(self._workers):
                if t.is_alive():
                    t.join(timeout=1.0)


# ---------------------------------------------------------------------------
# CLI entrypoint (parity with MCP tools for easy testing & scripting)
# ---------------------------------------------------------------------------


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex_bridge.py",
        description="Bridge Antigravity (AGY) to the Codex app-server daemon (MCP server + CLI).",
    )
    parser.add_argument("--mcp", action="store_true", help="Run as a stdio MCP server.")
    parser.add_argument("--sock", help="Custom Codex app-server Unix control socket path.")
    parser.add_argument(
        "--remote",
        help="Remote Codex app-server URL (wss://HOST:PORT, ws://127.0.0.1:PORT, or unix://PATH).",
    )
    parser.add_argument(
        "--auth-token",
        help="Optional bearer token (allowed only over wss://, unix://, or loopback ws://).",
    )

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Check daemon connectivity and loaded threads.")

    p_list = sub.add_parser("list", help="List Codex threads.")
    p_list.add_argument("--limit", type=int, default=15)
    p_list.add_argument("--cwd")
    p_list.add_argument("--search")
    p_list.add_argument("--loaded-only", action="store_true")
    p_list.add_argument(
        "--archived-only",
        "--archived",
        dest="archived_only",
        action="store_true",
        help="Return ONLY archived threads.",
    )
    p_list.add_argument(
        "--scan-rollouts",
        action="store_true",
        help="Allow scanning JSONL rollouts instead of indexed state DB only.",
    )
    p_list.add_argument(
        "--sort-key",
        default="updated_at",
        choices=["updated_at", "created_at", "recency_at"],
    )
    p_list.add_argument("--cursor")

    p_read = sub.add_parser("read", help="Read thread metadata and recent turns.")
    p_read.add_argument("thread_id")
    p_read.add_argument("--recent-turns", type=int, default=1)
    p_read.add_argument(
        "--items-view",
        default="compact",
        choices=["compact", "summary", "full", "notLoaded"],
    )
    p_read.add_argument("--include-tool-outputs", action="store_true")
    p_read.add_argument("--max-output-chars", type=int, default=8000)

    p_turns = sub.add_parser("turns", help="List paginated turns for a thread.")
    p_turns.add_argument("thread_id")
    p_turns.add_argument("--limit", type=int, default=5)
    p_turns.add_argument("--sort-direction", default="desc", choices=["desc", "asc"])
    p_turns.add_argument(
        "--items-view",
        default="compact",
        choices=["compact", "summary", "full", "notLoaded"],
    )
    p_turns.add_argument("--include-tool-outputs", action="store_true")
    p_turns.add_argument("--max-output-chars", type=int, default=8000)
    p_turns.add_argument("--cursor")

    p_turn = sub.add_parser("turn", help="Fetch full items (outputs + diffs) for one turn.")
    p_turn.add_argument("thread_id")
    p_turn.add_argument("turn_id")
    p_turn.add_argument("--max-output-chars", type=int, default=8000)
    p_turn.add_argument("--limit", type=int, default=100)
    p_turn.add_argument("--cursor")

    p_wait = sub.add_parser("wait", help="Wait for the active turn to finish or ask user.")
    p_wait.add_argument("thread_id")
    p_wait.add_argument("--turn-id")
    p_wait.add_argument("--timeout", type=float, default=DEFAULT_WAIT_TIMEOUT_SECONDS)
    p_wait.add_argument("--interval", type=float, default=1.5)
    p_wait.add_argument("--no-follow-new-turns", action="store_true")
    p_wait.add_argument("--return-on-user-steer", action="store_true")
    p_wait.add_argument("--include-tool-outputs", action="store_true")

    sub.add_parser("models", help="List available Codex models.")

    p_start = sub.add_parser("start", help="Start a new Codex thread.")
    p_start.add_argument("--cwd", required=True)
    p_start.add_argument("--name")
    p_start.add_argument("--prompt")
    p_start.add_argument("--model")
    p_start.add_argument("--effort")
    p_start.add_argument("--service-tier")
    p_start.add_argument(
        "--sandbox",
        default=None,
        choices=["read-only", "workspace-write", "danger-full-access"],
    )
    p_start.add_argument(
        "--approval-policy", default=None, choices=["on-request", "untrusted", "never"]
    )
    p_start.add_argument(
        "--approvals-reviewer", default=None, choices=["user", "auto_review"]
    )
    p_start.add_argument("--ephemeral", action="store_true")
    p_start.add_argument("--wait", action="store_true")
    p_start.add_argument("--timeout", type=float, default=DEFAULT_WAIT_TIMEOUT_SECONDS)

    p_fork = sub.add_parser("fork", help="Fork an existing thread into a new isolated thread.")
    p_fork.add_argument("thread_id")
    p_fork.add_argument("--name")
    p_fork.add_argument("--cwd")
    p_fork.add_argument("--service-tier")

    p_send = sub.add_parser("send", help="Send a prompt to an existing thread.")
    p_send.add_argument("thread_id")
    p_send.add_argument("prompt")
    p_send.add_argument("--yes", action="store_true", help="Confirm modifying this existing thread.")
    p_send.add_argument("--model")
    p_send.add_argument("--effort")
    p_send.add_argument("--service-tier")
    p_send.add_argument("--service-tier-for-turn")
    p_send.add_argument("--wait", action="store_true")
    p_send.add_argument("--timeout", type=float, default=DEFAULT_WAIT_TIMEOUT_SECONDS)

    p_steer = sub.add_parser("steer", help="Steer an active inProgress turn.")
    p_steer.add_argument("thread_id")
    p_steer.add_argument("prompt")
    p_steer.add_argument("--yes", action="store_true", help="Confirm steering this active thread.")
    p_steer.add_argument("--expected-turn-id")

    p_queue = sub.add_parser("queue", help="Queue a follow-up message via native `thread/queue/add`.")
    p_queue.add_argument("thread_id")
    p_queue.add_argument("message")
    p_queue.add_argument("--yes", action="store_true", help="Confirm queueing onto this thread.")
    p_queue.add_argument("--resume", action="store_true")

    p_int = sub.add_parser("interrupt", help="Request interruption of an active turn.")
    p_int.add_argument("thread_id")
    p_int.add_argument("--turn-id")
    p_int.add_argument("--yes", action="store_true", help="Confirm interrupting this thread.")

    p_rev = sub.add_parser("review", help="Start a native Codex code review.")
    p_rev.add_argument("--cwd")
    p_rev.add_argument(
        "--target",
        default="uncommittedChanges",
        choices=["uncommittedChanges", "baseBranch", "commit"],
    )
    p_rev.add_argument("--branch")
    p_rev.add_argument("--sha")
    p_rev.add_argument("--thread-id")
    p_rev.add_argument("--yes", action="store_true")
    p_rev.add_argument(
        "--confirm-replace-active-turn",
        action="store_true",
        help="Required with --thread-id and --yes to confirm replacing any running turn.",
    )
    p_rev.add_argument("--name")
    p_rev.add_argument("--model")
    p_rev.add_argument("--effort")
    p_rev.add_argument("--service-tier")
    p_rev.add_argument("--wait", action="store_true")
    p_rev.add_argument("--timeout", type=float, default=DEFAULT_WAIT_TIMEOUT_SECONDS)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_cli_parser()
    ns = parser.parse_args(argv)
    conn_kw = {
        "sock_path": ns.sock,
        "remote_url": ns.remote,
        "auth_token": ns.auth_token,
    }

    if ns.mcp:
        McpStdioServer(conn_kw).serve_forever()
        return 0

    if not ns.command:
        parser.print_help()
        return 2

    dispatch: Dict[
        str,
        Tuple[Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]], Dict[str, Any]],
    ] = {
        "status": (op_status, {}),
        "list": (
            op_list_threads,
            {
                "limit": getattr(ns, "limit", 15),
                "cwd": getattr(ns, "cwd", None),
                "search_term": getattr(ns, "search", None),
                "loaded_only": getattr(ns, "loaded_only", False),
                "archived_only": getattr(ns, "archived_only", False),
                "use_state_db_only": not getattr(ns, "scan_rollouts", False),
                "sort_key": getattr(ns, "sort_key", "updated_at"),
                "cursor": getattr(ns, "cursor", None),
            },
        ),
        "read": (
            op_read_thread,
            {
                "thread_id": getattr(ns, "thread_id", None),
                "recent_turns": getattr(ns, "recent_turns", 1),
                "items_view": getattr(ns, "items_view", "compact"),
                "include_tool_outputs": getattr(ns, "include_tool_outputs", False),
                "max_output_chars": getattr(ns, "max_output_chars", 8000),
            },
        ),
        "turns": (
            op_list_turns,
            {
                "thread_id": getattr(ns, "thread_id", None),
                "limit": getattr(ns, "limit", 5),
                "sort_direction": getattr(ns, "sort_direction", "desc"),
                "items_view": getattr(ns, "items_view", "compact"),
                "include_tool_outputs": getattr(ns, "include_tool_outputs", False),
                "max_output_chars": getattr(ns, "max_output_chars", 8000),
                "cursor": getattr(ns, "cursor", None),
            },
        ),
        "turn": (
            op_get_turn_details,
            {
                "thread_id": getattr(ns, "thread_id", None),
                "turn_id": getattr(ns, "turn_id", None),
                "max_output_chars": getattr(ns, "max_output_chars", 8000),
                "limit": getattr(ns, "limit", 100),
                "cursor": getattr(ns, "cursor", None),
            },
        ),
        "wait": (
            op_wait_turn,
            {
                "thread_id": getattr(ns, "thread_id", None),
                "turn_id": getattr(ns, "turn_id", None),
                "timeout_seconds": getattr(ns, "timeout", DEFAULT_WAIT_TIMEOUT_SECONDS),
                "poll_interval_seconds": getattr(ns, "interval", 1.5),
                "follow_new_turns": not getattr(ns, "no_follow_new_turns", False),
                "return_on_user_steer": getattr(ns, "return_on_user_steer", False),
                "include_tool_outputs": getattr(ns, "include_tool_outputs", False),
            },
        ),
        "models": (op_list_models, {}),
        "start": (
            op_start_thread,
            {
                "cwd": getattr(ns, "cwd", None),
                "name": getattr(ns, "name", None),
                "prompt": getattr(ns, "prompt", None),
                "model": getattr(ns, "model", None),
                "effort": getattr(ns, "effort", None),
                "service_tier": getattr(ns, "service_tier", None),
                "sandbox": getattr(ns, "sandbox", None),
                "approval_policy": getattr(ns, "approval_policy", None),
                "approvals_reviewer": getattr(ns, "approvals_reviewer", None),
                "ephemeral": getattr(ns, "ephemeral", False),
                "wait": getattr(ns, "wait", False),
                "timeout_seconds": getattr(ns, "timeout", DEFAULT_WAIT_TIMEOUT_SECONDS),
            },
        ),
        "fork": (
            op_fork_thread,
            {
                "thread_id": getattr(ns, "thread_id", None),
                "name": getattr(ns, "name", None),
                "cwd": getattr(ns, "cwd", None),
                "service_tier": getattr(ns, "service_tier", None),
            },
        ),
        "send": (
            op_send_turn,
            {
                "thread_id": getattr(ns, "thread_id", None),
                "prompt": getattr(ns, "prompt", None),
                "confirm_modify_thread": getattr(ns, "yes", False),
                "model": getattr(ns, "model", None),
                "effort": getattr(ns, "effort", None),
                "service_tier": getattr(ns, "service_tier", None),
                "service_tier_for_turn": getattr(ns, "service_tier_for_turn", None),
                "wait": getattr(ns, "wait", False),
                "timeout_seconds": getattr(ns, "timeout", DEFAULT_WAIT_TIMEOUT_SECONDS),
            },
        ),
        "steer": (
            op_steer_turn,
            {
                "thread_id": getattr(ns, "thread_id", None),
                "prompt": getattr(ns, "prompt", None),
                "confirm_modify_thread": getattr(ns, "yes", False),
                "expected_turn_id": getattr(ns, "expected_turn_id", None),
            },
        ),
        "queue": (
            op_queue_message,
            {
                "thread_id": getattr(ns, "thread_id", None),
                "message": getattr(ns, "message", None),
                "confirm_modify_thread": getattr(ns, "yes", False),
                "resume": getattr(ns, "resume", False),
            },
        ),
        "interrupt": (
            op_interrupt_turn,
            {
                "thread_id": getattr(ns, "thread_id", None),
                "turn_id": getattr(ns, "turn_id", None),
                "confirm_modify_thread": getattr(ns, "yes", False),
            },
        ),
        "review": (
            op_start_review,
            {
                "cwd": getattr(ns, "cwd", None),
                "target_type": getattr(ns, "target", "uncommittedChanges"),
                "branch": getattr(ns, "branch", None),
                "sha": getattr(ns, "sha", None),
                "thread_id": getattr(ns, "thread_id", None),
                "confirm_modify_thread": getattr(ns, "yes", False),
                "confirm_replace_active_turn": getattr(
                    ns, "confirm_replace_active_turn", False
                ),
                "name": getattr(ns, "name", None),
                "model": getattr(ns, "model", None),
                "effort": getattr(ns, "effort", None),
                "service_tier": getattr(ns, "service_tier", None),
                "wait": getattr(ns, "wait", False),
                "timeout_seconds": getattr(ns, "timeout", DEFAULT_WAIT_TIMEOUT_SECONDS),
            },
        ),
    }

    fn, args = dispatch[ns.command]
    try:
        result = fn(args, conn_kw)
    except CodexPartialSuccessError as exc:
        print(
            json.dumps(exc.partial_result, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
