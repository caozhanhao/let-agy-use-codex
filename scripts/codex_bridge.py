#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from codex_app_client import (  # noqa: E402
    CLIENT_NAME,
    CLIENT_VERSION,
    CodexAppClient,
    CodexBridgeError,
    find_codex_binary,
)

TERMINAL_TURN_STATUSES = {"completed", "failed", "interrupted"}
SUPPORTED_MCP_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")


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
            if turn_idx == 0 and um_idx > 0:
                entry: Dict[str, Any] = {
                    "kind": "mid_turn_steer",
                    "turnId": tid,
                    "itemId": it.get("id"),
                    "text": text,
                }
                if truncated:
                    entry["truncated"] = True
                interventions.append(entry)
            elif turn_idx > 0:
                entry = {
                    "kind": "follow_up_turn",
                    "turnId": tid,
                    "itemId": it.get("id"),
                    "text": text,
                }
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


def format_item(
    item: Dict[str, Any],
    *,
    include_tool_outputs: bool = False,
    max_output_chars: int = 8000,
) -> Optional[Dict[str, Any]]:
    """Format a Codex ThreadItem cleanly while keeping token cost tight."""
    item_type = item.get("type", "unknown")

    if item_type == "userMessage":
        text, truncated = _truncate(_extract_user_message_text(item), 6000)
        out: Dict[str, Any] = {"type": "userMessage", "id": item.get("id"), "text": text}
        if truncated:
            out["truncated"] = True
        return out

    if item_type == "agentMessage":
        text, truncated = _truncate(
            str(item.get("text") or ""),
            max_output_chars if include_tool_outputs else 8000,
        )
        out = {"type": "agentMessage", "id": item.get("id"), "text": text}
        if truncated:
            out["truncated"] = True
        return out

    if item_type == "reasoning":
        summary = [str(s) for s in (item.get("summary") or []) if str(s).strip()]
        if not summary:
            return None  # Drop empty reasoning placeholders to save tokens.
        return {"type": "reasoning", "id": item.get("id"), "summary": summary}

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
            text, truncated = _truncate(agg, max_output_chars)
            out["output"] = text
            if truncated:
                out["truncated"] = True
        elif item.get("exitCode") not in (None, 0) and agg:
            text, truncated = _truncate(agg, 1200)
            out["outputPreview"] = text
            if truncated:
                out["truncated"] = True
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
                text, truncated = _truncate(diff, max_output_chars)
                entry["diff"] = text
                if truncated:
                    entry["truncated"] = True
            changes.append(entry)
        return {
            "type": "fileChange",
            "id": item.get("id"),
            "status": item.get("status"),
            "changes": changes,
        }

    if item_type in ("mcpToolCall", "collabAgentToolCall"):
        out = {
            "type": item_type,
            "id": item.get("id"),
            "status": item.get("status"),
            "tool": item.get("tool") or item.get("name"),
            "server": item.get("server"),
        }
        if include_tool_outputs and item.get("result") is not None:
            raw_res = json.dumps(item.get("result"), ensure_ascii=False)
            text, truncated = _truncate(raw_res, max_output_chars)
            out["result"] = text
            if truncated:
                out["truncated"] = True
        if item.get("error") is not None:
            out["error"] = item.get("error")
        return {k: v for k, v in out.items() if v is not None}

    if item_type == "webSearch":
        return {
            "type": "webSearch",
            "id": item.get("id"),
            "query": item.get("query"),
            "status": item.get("status"),
        }

    # Generic fallback for contextCompaction, imageView, subAgentActivity, etc.
    compact: Dict[str, Any] = {"type": item_type, "id": item.get("id")}
    for k in ("status", "name", "path", "text", "summary", "error"):
        if item.get(k) is not None:
            val = item[k]
            if isinstance(val, str):
                val, _ = _truncate(val, 1000)
            compact[k] = val
    return compact


def format_turn(
    turn: Dict[str, Any],
    *,
    include_tool_outputs: bool = False,
    max_output_chars: int = 8000,
) -> Dict[str, Any]:
    raw_items = turn.get("items") or []
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
    if turn.get("error") is not None:
        out["error"] = turn.get("error")
    return out


def format_thread_summary(
    thread: Dict[str, Any],
    *,
    loaded_ids: Optional[set] = None,
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


def _ensure_loaded(client: CodexAppClient, thread_id: str) -> Dict[str, Any]:
    meta = client.call("thread/read", {"threadId": thread_id, "includeTurns": False})
    thread = (meta or {}).get("thread") or {}
    status = thread.get("status")
    is_not_loaded = isinstance(status, dict) and status.get("type") == "notLoaded"
    if is_not_loaded:
        client.call(
            "thread/resume",
            {"threadId": thread_id, "excludeTurns": True},
        )
        meta = client.call("thread/read", {"threadId": thread_id, "includeTurns": False})
        thread = (meta or {}).get("thread") or {}
    return thread


def _slice_turn_window(
    turns_desc: List[Dict[str, Any]], anchor_turn_id: Optional[str]
) -> List[Dict[str, Any]]:
    """Return chronological turns from anchor_turn_id through the latest turn."""
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
        return [turns_desc[0]]
    collected.reverse()
    return collected


def _wait_for_turn_readonly(
    client: CodexAppClient,
    thread_id: str,
    *,
    turn_id: Optional[str] = None,
    timeout_seconds: float = 180.0,
    poll_interval_seconds: float = 1.5,
    return_on_user_wait: bool = True,
    follow_new_turns: bool = True,
    return_on_user_steer: bool = False,
    include_tool_outputs: bool = False,
) -> Dict[str, Any]:
    timeout_seconds = max(1.0, min(float(timeout_seconds), 600.0))
    poll_interval_seconds = max(0.5, min(float(poll_interval_seconds), 10.0))
    start = time.monotonic()
    deadline = start + timeout_seconds
    anchor_turn_id = turn_id
    initial_user_msg_count: Optional[int] = None
    active_idle_grace_start: Optional[float] = None

    def _hydrate_result(
        outcome: str,
        thread: Dict[str, Any],
        window_count: int,
        *,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        fetch_limit = max(window_count + 2, 5)
        full_res = client.call(
            "thread/turns/list",
            {
                "threadId": thread_id,
                "limit": fetch_limit,
                "sortDirection": "desc",
                "itemsView": "full",
            },
        ) or {}
        full_desc = full_res.get("data") or full_res.get("turns") or []
        final_window = _slice_turn_window(full_desc, anchor_turn_id)
        interventions = _extract_user_interventions(final_window)
        formatted_turns = [
            format_turn(tr, include_tool_outputs=include_tool_outputs)
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
        if extra:
            result.update(extra)
        return result

    while True:
        meta = client.call("thread/read", {"threadId": thread_id, "includeTurns": False})
        thread = (meta or {}).get("thread") or {}
        flags = _active_flags(thread)
        status_obj = thread.get("status")
        thread_active = isinstance(status_obj, dict) and status_obj.get("type") == "active"

        turns_res = client.call(
            "thread/turns/list",
            {
                "threadId": thread_id,
                "limit": 10,
                "sortDirection": "desc",
                "itemsView": "full" if return_on_user_steer else "notLoaded",
            },
        ) or {}
        turns_desc = turns_res.get("data") or turns_res.get("turns") or []
        if anchor_turn_id is None and turns_desc:
            anchor_turn_id = turns_desc[0].get("id")
        window = _slice_turn_window(turns_desc, anchor_turn_id)

        if return_on_user_steer and window:
            anchor_ums = sum(
                1
                for it in (window[0].get("items") or [])
                if isinstance(it, dict) and it.get("type") == "userMessage"
            )
            if initial_user_msg_count is None:
                initial_user_msg_count = anchor_ums
            elif anchor_ums > initial_user_msg_count or len(window) > 1:
                return _hydrate_result(
                    "user_steered",
                    thread,
                    len(window),
                    extra={
                        "message": (
                            "User injected a mid-turn steer or follow-up message in Codex. "
                            "Inspect `userInterventions` and continue waiting if appropriate."
                        )
                    },
                )

        if return_on_user_wait and flags:
            return _hydrate_result(
                "waiting_for_user",
                thread,
                len(window),
                extra={
                    "activeFlags": flags,
                    "message": (
                        "Codex is waiting for human input/approval in the Codex UI. "
                        "Ask the user to approve or respond in Codex, then call codex_wait_turn again."
                    ),
                },
            )

        if window:
            anchor_turn = window[0]
            latest_turn = window[-1]
            any_in_progress = any(tr.get("status") == "inProgress" for tr in window)

            if not follow_new_turns:
                if anchor_turn.get("status") in TERMINAL_TURN_STATUSES:
                    return _hydrate_result(
                        str(anchor_turn.get("status")), thread, 1
                    )
            else:
                if not any_in_progress and latest_turn.get("status") in TERMINAL_TURN_STATUSES:
                    if not thread_active:
                        return _hydrate_result(
                            str(latest_turn.get("status")), thread, len(window)
                        )
                    # Thread reports active while no turn is inProgress (e.g. queued
                    # turn starting or brief status lag). Wait briefly before exiting.
                    now = time.monotonic()
                    if active_idle_grace_start is None:
                        active_idle_grace_start = now
                    elif now - active_idle_grace_start >= 5.0:
                        return _hydrate_result(
                            str(latest_turn.get("status")), thread, len(window)
                        )
                else:
                    active_idle_grace_start = None

        if time.monotonic() >= deadline:
            return _hydrate_result("timeout", thread, len(window))

        time.sleep(min(poll_interval_seconds, max(0.1, deadline - time.monotonic())))


# ---------------------------------------------------------------------------
# Tool implementations (shared by both MCP server and CLI)
# ---------------------------------------------------------------------------


def op_status(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    with CodexAppClient(**conn_kw) as client:
        loaded = client.call("thread/loaded/list", {})
        loaded_ids = (loaded or {}).get("data") or []
        return {
            "connected": True,
            "handshake": client.handshake_status,
            "server": client.server_info,
            "loadedThreadCount": len(loaded_ids),
            "loadedThreadIds": loaded_ids,
        }


def op_list_threads(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    limit = int(args.get("limit") or 15)
    limit = max(1, min(limit, 100))
    sort_key = args.get("sort_key") or "updated_at"
    loaded_only = bool(args.get("loaded_only", False))

    params: Dict[str, Any] = {
        "limit": limit,
        "sortKey": sort_key,
        "archived": bool(args.get("include_archived", False)),
    }
    if args.get("cwd"):
        params["cwd"] = args["cwd"]
    if args.get("search_term"):
        params["searchTerm"] = args["search_term"]
    if args.get("cursor"):
        params["cursor"] = args["cursor"]

    with CodexAppClient(**conn_kw) as client:
        loaded = client.call("thread/loaded/list", {})
        loaded_ids = set((loaded or {}).get("data") or [])
        if loaded_only:
            if not loaded_ids:
                return {"threads": [], "loadedCount": 0, "nextCursor": None}
            params["includedThreadIds"] = sorted(loaded_ids)

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
    recent_turns = int(args.get("recent_turns", 1))
    recent_turns = max(0, min(recent_turns, 20))
    items_view = str(args.get("items_view") or "compact")
    include_tool_outputs = bool(args.get("include_tool_outputs", False))
    rpc_items_view = _resolve_rpc_items_view(items_view, include_tool_outputs)

    with CodexAppClient(**conn_kw) as client:
        loaded = client.call("thread/loaded/list", {})
        loaded_ids = set((loaded or {}).get("data") or [])
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
            out["recentTurns"] = [
                format_turn(tr, include_tool_outputs=include_tool_outputs)
                for tr in turns
            ]
            out["nextCursor"] = turns_res.get("nextCursor")
        return out


def op_list_turns(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    thread_id = str(args["thread_id"])
    limit = max(1, min(int(args.get("limit") or 5), 50))
    sort_direction = str(args.get("sort_direction") or "desc")
    items_view = str(args.get("items_view") or "compact")
    include_tool_outputs = bool(args.get("include_tool_outputs", False))
    rpc_items_view = _resolve_rpc_items_view(items_view, include_tool_outputs)

    params: Dict[str, Any] = {
        "threadId": thread_id,
        "limit": limit,
        "sortDirection": sort_direction,
        "itemsView": rpc_items_view,
    }
    if args.get("cursor"):
        params["cursor"] = args["cursor"]

    with CodexAppClient(**conn_kw) as client:
        res = client.call("thread/turns/list", params) or {}
        turns = res.get("data") or res.get("turns") or []
        return {
            "threadId": thread_id,
            "count": len(turns),
            "turns": [
                format_turn(tr, include_tool_outputs=include_tool_outputs)
                for tr in turns
            ],
            "nextCursor": res.get("nextCursor"),
            "backwardsCursor": res.get("backwardsCursor"),
        }


def op_get_turn_details(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    thread_id = str(args["thread_id"])
    turn_id = str(args["turn_id"])
    max_output_chars = max(500, min(int(args.get("max_output_chars") or 8000), 50000))
    limit = max(1, min(int(args.get("limit") or 100), 200))

    params: Dict[str, Any] = {
        "threadId": thread_id,
        "turnId": turn_id,
        "limit": limit,
        "sortDirection": "asc",
    }
    if args.get("cursor"):
        params["cursor"] = args["cursor"]

    with CodexAppClient(**conn_kw) as client:
        res = client.call("thread/items/list", params) or {}
        raw_items = res.get("data") or []
        formatted = []
        for it in raw_items:
            if isinstance(it, dict):
                fmt = format_item(
                    it,
                    include_tool_outputs=True,
                    max_output_chars=max_output_chars,
                )
                if fmt is not None:
                    formatted.append(fmt)
        return {
            "threadId": thread_id,
            "turnId": turn_id,
            "count": len(formatted),
            "items": formatted,
            "nextCursor": res.get("nextCursor"),
        }


def op_wait_turn(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    thread_id = str(args["thread_id"])
    with CodexAppClient(**conn_kw) as client:
        return _wait_for_turn_readonly(
            client,
            thread_id,
            turn_id=args.get("turn_id"),
            timeout_seconds=float(args.get("timeout_seconds", 180)),
            poll_interval_seconds=float(args.get("poll_interval_seconds", 1.5)),
            return_on_user_wait=bool(args.get("return_on_user_wait", True)),
            follow_new_turns=bool(args.get("follow_new_turns", True)),
            return_on_user_steer=bool(args.get("return_on_user_steer", False)),
            include_tool_outputs=bool(args.get("include_tool_outputs", False)),
        )


def op_list_models(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    with CodexAppClient(**conn_kw) as client:
        res = client.call("model/list", {}) or {}
        models = res.get("data") or res.get("models") or []
        compact = []
        for m in models:
            if isinstance(m, dict):
                compact.append(
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
        return {"count": len(compact), "models": compact}


def op_start_thread(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    cwd = os.path.abspath(os.path.expanduser(str(args["cwd"])))
    if not os.path.isdir(cwd):
        raise CodexBridgeError(f"cwd does not exist or is not a directory: {cwd}")

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
        start_params["ephemeral"] = bool(args["ephemeral"])

    with CodexAppClient(**conn_kw) as client:
        res = client.call("thread/start", start_params) or {}
        thread = res.get("thread") or {}
        thread_id = thread.get("id")
        if not thread_id:
            raise CodexBridgeError(f"thread/start did not return a thread id: {res}")

        name = args.get("name")
        if name:
            client.call("thread/name/set", {"threadId": thread_id, "name": str(name)})
            thread["name"] = str(name)

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
            "resumeCommand": f"codex resume {thread_id}",
        }

        prompt = args.get("prompt")
        if prompt:
            turn_params: Dict[str, Any] = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": str(prompt)}],
            }
            if args.get("effort"):
                turn_params["effort"] = str(args["effort"])
            turn_res = client.call("turn/start", turn_params) or {}
            turn = turn_res.get("turn") or {}
            out["turnId"] = turn.get("id")
            out["turnStatus"] = turn.get("status")
            if args.get("wait"):
                out["waitResult"] = _wait_for_turn_readonly(
                    client,
                    thread_id,
                    turn_id=turn.get("id"),
                    timeout_seconds=float(args.get("timeout_seconds", 180)),
                    include_tool_outputs=bool(args.get("include_tool_outputs", False)),
                )
        return out


def op_fork_thread(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    thread_id = str(args["thread_id"])
    fork_params: Dict[str, Any] = {
        "threadId": thread_id,
        "excludeTurns": True,
    }
    if args.get("cwd"):
        fork_params["cwd"] = os.path.abspath(os.path.expanduser(str(args["cwd"])))
    if args.get("sandbox"):
        fork_params["sandbox"] = str(args["sandbox"])
    if args.get("approval_policy"):
        fork_params["approvalPolicy"] = str(args["approval_policy"])
    if args.get("approvals_reviewer"):
        fork_params["approvalsReviewer"] = str(args["approvals_reviewer"])
    service_tier = _normalize_service_tier(args.get("service_tier"))
    if service_tier:
        fork_params["serviceTier"] = service_tier

    with CodexAppClient(**conn_kw) as client:
        res = client.call("thread/fork", fork_params) or {}
        thread = res.get("thread") or {}
        new_id = thread.get("id")
        if not new_id:
            raise CodexBridgeError(f"thread/fork did not return a new thread id: {res}")
        if args.get("name"):
            client.call("thread/name/set", {"threadId": new_id, "name": str(args["name"])})
            thread["name"] = str(args["name"])
        return {
            "forkedFromId": thread_id,
            "threadId": new_id,
            "name": thread.get("name"),
            "cwd": res.get("cwd") or thread.get("cwd"),
            "model": res.get("model") or thread.get("model"),
            "serviceTier": res.get("serviceTier"),
            "resumeCommand": f"codex resume {new_id}",
        }


def op_send_turn(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    _require_modify_confirmation(args, "codex_send_turn")
    thread_id = str(args["thread_id"])
    prompt = str(args["prompt"])

    with CodexAppClient(**conn_kw) as client:
        _ensure_loaded(client, thread_id)
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
        if args.get("wait"):
            out["waitResult"] = _wait_for_turn_readonly(
                client,
                thread_id,
                turn_id=turn.get("id"),
                timeout_seconds=float(args.get("timeout_seconds", 180)),
                include_tool_outputs=bool(args.get("include_tool_outputs", False)),
            )
        return out


def op_steer_turn(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    _require_modify_confirmation(args, "codex_steer_turn")
    thread_id = str(args["thread_id"])
    prompt = str(args["prompt"])

    with CodexAppClient(**conn_kw) as client:
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

    with CodexAppClient(**conn_kw) as client:
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
                    "interrupted": False,
                    "message": "No inProgress turn found on this thread.",
                }
            turn_id = latest.get("id")

        client.call("turn/interrupt", {"threadId": thread_id, "turnId": str(turn_id)})
        return {"threadId": thread_id, "turnId": str(turn_id), "interrupted": True}


def op_queue_message(args: Dict[str, Any], conn_kw: Dict[str, Any]) -> Dict[str, Any]:
    _require_modify_confirmation(args, "codex_queue_message")
    thread_id = str(args["thread_id"])
    message = str(args["message"])
    codex_bin = find_codex_binary()
    cmd = [codex_bin, "queue", "--thread", thread_id, "--message", message]
    if args.get("resume"):
        cmd.append("--resume")
    remote_url = conn_kw.get("remote_url") or os.environ.get("CODEX_REMOTE_URL")
    if remote_url:
        cmd.extend(["--remote", remote_url])

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise CodexBridgeError(
            f"`codex queue` failed (rc={proc.returncode}): {(proc.stderr or proc.stdout).strip()}"
        )
    return {
        "threadId": thread_id,
        "queued": True,
        "output": (proc.stdout or "").strip(),
    }


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
    created_new = False
    started_meta: Dict[str, Any] = {}
    with CodexAppClient(**conn_kw) as client:
        if thread_id:
            _require_modify_confirmation(args, "codex_start_review (on existing thread)")
            thread_id = str(thread_id)
            _ensure_loaded(client, thread_id)
        else:
            cwd_arg = args.get("cwd")
            if not cwd_arg:
                raise CodexBridgeError("`cwd` is required when `thread_id` is omitted")
            cwd = os.path.abspath(os.path.expanduser(str(cwd_arg)))
            if not os.path.isdir(cwd):
                raise CodexBridgeError(f"cwd does not exist: {cwd}")
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
            client.call("thread/name/set", {"threadId": thread_id, "name": title})

        res = client.call(
            "review/start",
            {"threadId": thread_id, "target": target, "delivery": "inline"},
        ) or {}
        turn = res.get("turn") or {}
        out: Dict[str, Any] = {
            "threadId": thread_id,
            "createdNewThread": created_new,
            "target": target,
            "turnId": turn.get("id"),
            "turnStatus": turn.get("status"),
            "resumeCommand": f"codex resume {thread_id}",
        }
        if created_new:
            out["model"] = started_meta.get("model")
            out["reasoningEffort"] = started_meta.get("reasoningEffort")
            out["serviceTier"] = started_meta.get("serviceTier")
        if args.get("wait"):
            out["waitResult"] = _wait_for_turn_readonly(
                client,
                thread_id,
                turn_id=turn.get("id"),
                timeout_seconds=float(args.get("timeout_seconds", 240)),
                include_tool_outputs=bool(args.get("include_tool_outputs", False)),
            )
        return out


# ---------------------------------------------------------------------------
# MCP Tool Registry
# ---------------------------------------------------------------------------


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "codex_status",
        "description": "Check connectivity to the local Codex app-server daemon and list currently loaded thread IDs. Read-only.",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": op_status,
    },
    {
        "name": "codex_list_threads",
        "description": "List Codex threads from the shared app-server daemon with compact metadata (id, name, status, isLoaded, model, cwd, updatedAt, short preview). Read-only and token-efficient.",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 15},
                "cwd": {"type": "string", "description": "Optional exact working directory filter."},
                "search_term": {"type": "string", "description": "Optional search keyword."},
                "loaded_only": {"type": "boolean", "default": False, "description": "Only return threads currently loaded in daemon memory."},
                "include_archived": {"type": "boolean", "default": False},
                "sort_key": {"type": "string", "enum": ["updated_at", "created_at", "recency_at"], "default": "updated_at"},
                "cursor": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "handler": op_list_threads,
    },
    {
        "name": "codex_read_thread",
        "description": "Read metadata, live status/activeFlags (including waitingOnApproval/waitingOnUserInput), and the latest N turns of a Codex thread. Pure read-only; defaults to recent_turns=1 and items_view='compact' (preserves ALL userMessages including mid-turn steers while compacting heavy tool outputs).",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {
                "thread_id": {"type": "string"},
                "recent_turns": {"type": "integer", "minimum": 0, "maximum": 20, "default": 1},
                "items_view": {"type": "string", "enum": ["compact", "summary", "full", "notLoaded"], "default": "compact"},
                "include_tool_outputs": {"type": "boolean", "default": False, "description": "Include raw command stdout and file diffs (larger response)."},
            },
            "additionalProperties": False,
        },
        "handler": op_read_thread,
    },
    {
        "name": "codex_list_turns",
        "description": "Paginate turn history of a Codex thread. Pure read-only. Defaults to newest-first (desc), limit=5, compact view (preserves all userMessages & mid-turn steers while compacting heavy tool outputs).",
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
                "cursor": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "handler": op_list_turns,
    },
    {
        "name": "codex_get_turn_details",
        "description": "Fetch full items for a single Codex turn, including raw command outputs and file diffs (capped per item by max_output_chars). Pure read-only.",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "turn_id"],
            "properties": {
                "thread_id": {"type": "string"},
                "turn_id": {"type": "string"},
                "max_output_chars": {"type": "integer", "minimum": 500, "maximum": 50000, "default": 8000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100},
                "cursor": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "handler": op_get_turn_details,
    },
    {
        "name": "codex_wait_turn",
        "description": "Wait (pure read-only, 0 LLM polling tokens) until Codex finishes the active turn (and any user-steered/queued follow-up turns when follow_new_turns=true) or enters waitingOnApproval/waitingOnUserInput. Always hydrates full turn items on completion so `userInterventions` and `turn.midTurnSteers` expose every message the user typed in Codex mid-flight.",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {
                "thread_id": {"type": "string"},
                "turn_id": {"type": "string", "description": "Optional starting turn ID; defaults to the latest turn."},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 600, "default": 180},
                "poll_interval_seconds": {"type": "number", "minimum": 0.5, "maximum": 10, "default": 1.5},
                "return_on_user_wait": {"type": "boolean", "default": True},
                "follow_new_turns": {"type": "boolean", "default": True, "description": "Keep waiting if the user steers/queues a follow-up turn while the thread stays active."},
                "return_on_user_steer": {"type": "boolean", "default": False, "description": "If true, return immediately with waitOutcome='user_steered' when a human injects a mid-turn steer or follow-up message."},
                "include_tool_outputs": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "handler": op_wait_turn,
    },
    {
        "name": "codex_list_models",
        "description": "List available Codex models, reasoning efforts, and service tiers (e.g. fast) advertised by the running Codex app-server. Read-only.",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": op_list_models,
    },
    {
        "name": "codex_start_thread",
        "description": "Create a NEW Codex thread on the shared daemon (visible immediately in Codex Desktop / IDE / CLI) and optionally send an initial prompt. Safe default for AGY-owned delegated tasks.",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
        "inputSchema": {
            "type": "object",
            "required": ["cwd"],
            "properties": {
                "cwd": {"type": "string", "description": "Absolute working directory for the Codex thread."},
                "name": {"type": "string", "description": "Human-readable title shown in Codex UI sidebar."},
                "prompt": {"type": "string", "description": "Optional initial prompt to start immediately."},
                "model": {"type": "string"},
                "effort": {"type": "string", "description": "Reasoning effort override (e.g. medium, high, xhigh)."},
                "service_tier": {"type": "string", "description": "Optional service tier override (e.g. 'fast'/'priority' for 2x Fast mode, or 'default' for standard); omit to inherit ~/.codex/config.toml default."},
                "sandbox": {"type": "string", "enum": ["read-only", "workspace-write", "danger-full-access"], "description": "Optional sandbox override; omit to inherit ~/.codex/config.toml default."},
                "approval_policy": {"type": "string", "enum": ["on-request", "untrusted", "never"], "description": "Optional approval policy override; omit to inherit ~/.codex/config.toml default."},
                "approvals_reviewer": {"type": "string", "enum": ["user", "auto_review"], "description": "Optional approvals reviewer override; omit to inherit ~/.codex/config.toml default (e.g. Approve for me)."},
                "ephemeral": {"type": "boolean", "default": False},
                "wait": {"type": "boolean", "default": False, "description": "Wait for the initial turn to finish before returning."},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 600, "default": 180},
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
                "service_tier": {"type": "string", "description": "Optional service tier override (e.g. 'fast' or 'default')."},
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
        "description": "Send a new prompt to an existing idle Codex thread. Refuses if a turn is currently inProgress. GUARDRAIL: Only set confirm_modify_thread=true for threads created by AGY or when the user explicitly asked to prompt this thread.",
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
                "service_tier_for_turn": {"type": "string", "description": "Override the service tier for THIS turn only without changing the thread default (e.g. 'fast'/'priority' or 'default')."},
                "approval_policy": {"type": "string", "enum": ["on-request", "untrusted", "never"]},
                "wait": {"type": "boolean", "default": False},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 600, "default": 180},
                "include_tool_outputs": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "handler": op_send_turn,
    },
    {
        "name": "codex_steer_turn",
        "description": "Steer an actively running (inProgress) Codex turn mid-flight with a course-correction message. GUARDRAIL: Requires confirm_modify_thread=true.",
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
        "description": "Queue a follow-up message on an existing Codex thread via `codex queue` without interrupting an ongoing turn. GUARDRAIL: Requires confirm_modify_thread=true.",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
        "inputSchema": {
            "type": "object",
            "required": ["thread_id", "message", "confirm_modify_thread"],
            "properties": {
                "thread_id": {"type": "string"},
                "message": {"type": "string"},
                "confirm_modify_thread": {"type": "boolean"},
                "resume": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "handler": op_queue_message,
    },
    {
        "name": "codex_interrupt_turn",
        "description": "Interrupt a currently running Codex turn. GUARDRAIL: Requires confirm_modify_thread=true.",
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
        "description": "Start a native Codex code review (uncommittedChanges, baseBranch, or commit). When thread_id is omitted, safely creates a new read-only Codex thread visible in the user's Codex UI.",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "description": "Absolute repo path (required when thread_id is omitted)."},
                "target_type": {"type": "string", "enum": ["uncommittedChanges", "baseBranch", "commit"], "default": "uncommittedChanges"},
                "branch": {"type": "string", "description": "Base branch name (required for target_type='baseBranch')."},
                "sha": {"type": "string", "description": "Commit SHA (required for target_type='commit')."},
                "thread_id": {"type": "string", "description": "Optional existing thread ID; if set, confirm_modify_thread=true is required."},
                "confirm_modify_thread": {"type": "boolean", "default": False},
                "name": {"type": "string"},
                "model": {"type": "string", "description": "Optional model override when creating a new review thread."},
                "effort": {"type": "string", "description": "Optional reasoning effort override when creating a new review thread (e.g. medium, high, xhigh)."},
                "service_tier": {"type": "string", "description": "Optional service tier override when creating a new review thread (e.g. 'fast' or 'default')."},
                "wait": {"type": "boolean", "default": False},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 600, "default": 240},
                "include_tool_outputs": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "handler": op_start_review,
    },
]

TOOL_MAP: Dict[str, Dict[str, Any]] = {t["name"]: t for t in TOOLS}


# ---------------------------------------------------------------------------
# Pure-stdlib MCP stdio server (supports both JSONL and Content-Length framing)
# ---------------------------------------------------------------------------


class McpStdioServer:
    def __init__(self, conn_kw: Dict[str, Any]):
        self.conn_kw = conn_kw
        self.framing = "jsonl"

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

    def handle_request(self, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = req.get("method")
        req_id = req.get("id")
        params = req.get("params") or {}
        is_notification = "id" not in req

        if method == "notifications/initialized" or (is_notification and method != "initialize"):
            return None

        if method == "initialize":
            client_proto = params.get("protocolVersion")
            proto = (
                client_proto
                if client_proto in SUPPORTED_MCP_PROTOCOLS
                else SUPPORTED_MCP_PROTOCOLS[1]
            )
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": proto,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                    "instructions": (
                        "Bridge to the local OpenAI Codex app-server daemon. "
                        "Read-only tools (codex_status, codex_list_threads, codex_read_thread, "
                        "codex_list_turns, codex_get_turn_details, codex_wait_turn, codex_list_models) "
                        "never alter Codex state. Never send/steer/queue/interrupt a user's existing "
                        "thread unless explicitly instructed by the user; prefer codex_start_thread "
                        "or codex_fork_thread for AGY-initiated work."
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
            tool_args = params.get("arguments") or {}
            tool = TOOL_MAP.get(str(tool_name))
            if tool is None:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                        "isError": True,
                    },
                }
            try:
                data = tool["handler"](tool_args, self.conn_kw)
                text = json.dumps(data, ensure_ascii=False, indent=2)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": text}],
                        "isError": False,
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

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    def serve_forever(self) -> None:
        while True:
            try:
                req = self._read_message()
            except Exception as exc:
                print(f"[let-agy-use-codex] MCP read error: {exc}", file=sys.stderr)
                continue
            if req is None:
                break
            resp = self.handle_request(req)
            if resp is not None:
                self._write_message(resp)


# ---------------------------------------------------------------------------
# CLI entrypoint (parity with MCP tools for easy testing & scripting)
# ---------------------------------------------------------------------------


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex_bridge.py",
        description="Bridge Antigravity (AGY) to the local Codex app-server daemon (MCP server + CLI).",
    )
    parser.add_argument("--mcp", action="store_true", help="Run as a stdio MCP server.")
    parser.add_argument("--sock", help="Custom Codex app-server Unix control socket path.")
    parser.add_argument("--remote", help="Remote Codex app-server URL (ws://HOST:PORT or unix://PATH).")
    parser.add_argument("--auth-token", help="Optional bearer token for ws:// remote server.")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Check daemon connectivity and loaded threads.")

    p_list = sub.add_parser("list", help="List Codex threads.")
    p_list.add_argument("--limit", type=int, default=15)
    p_list.add_argument("--cwd")
    p_list.add_argument("--search")
    p_list.add_argument("--loaded-only", action="store_true")
    p_list.add_argument("--archived", action="store_true")
    p_list.add_argument("--sort-key", default="updated_at", choices=["updated_at", "created_at", "recency_at"])
    p_list.add_argument("--cursor")

    p_read = sub.add_parser("read", help="Read thread metadata and recent turns.")
    p_read.add_argument("thread_id")
    p_read.add_argument("--recent-turns", type=int, default=1)
    p_read.add_argument("--items-view", default="compact", choices=["compact", "summary", "full", "notLoaded"])
    p_read.add_argument("--include-tool-outputs", action="store_true")

    p_turns = sub.add_parser("turns", help="List paginated turns for a thread.")
    p_turns.add_argument("thread_id")
    p_turns.add_argument("--limit", type=int, default=5)
    p_turns.add_argument("--sort-direction", default="desc", choices=["desc", "asc"])
    p_turns.add_argument("--items-view", default="compact", choices=["compact", "summary", "full", "notLoaded"])
    p_turns.add_argument("--include-tool-outputs", action="store_true")
    p_turns.add_argument("--cursor")

    p_turn = sub.add_parser("turn", help="Fetch full items (outputs + diffs) for one turn.")
    p_turn.add_argument("thread_id")
    p_turn.add_argument("turn_id")
    p_turn.add_argument("--max-output-chars", type=int, default=8000)

    p_wait = sub.add_parser("wait", help="Wait read-only for the active turn to finish or ask user.")
    p_wait.add_argument("thread_id")
    p_wait.add_argument("--turn-id")
    p_wait.add_argument("--timeout", type=float, default=180.0)
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
    p_start.add_argument("--sandbox", default=None, choices=["read-only", "workspace-write", "danger-full-access"])
    p_start.add_argument("--approval-policy", default=None, choices=["on-request", "untrusted", "never"])
    p_start.add_argument("--approvals-reviewer", default=None, choices=["user", "auto_review"])
    p_start.add_argument("--wait", action="store_true")
    p_start.add_argument("--timeout", type=float, default=180.0)

    p_fork = sub.add_parser("fork", help="Fork an existing thread into a new isolated thread.")
    p_fork.add_argument("thread_id")
    p_fork.add_argument("--name")
    p_fork.add_argument("--cwd")
    p_fork.add_argument("--service-tier")

    p_send = sub.add_parser("send", help="Send a prompt to an existing idle thread.")
    p_send.add_argument("thread_id")
    p_send.add_argument("prompt")
    p_send.add_argument("--yes", action="store_true", help="Confirm modifying this existing thread.")
    p_send.add_argument("--model")
    p_send.add_argument("--effort")
    p_send.add_argument("--service-tier")
    p_send.add_argument("--service-tier-for-turn")
    p_send.add_argument("--wait", action="store_true")
    p_send.add_argument("--timeout", type=float, default=180.0)

    p_steer = sub.add_parser("steer", help="Steer an active inProgress turn.")
    p_steer.add_argument("thread_id")
    p_steer.add_argument("prompt")
    p_steer.add_argument("--yes", action="store_true", help="Confirm steering this active thread.")
    p_steer.add_argument("--expected-turn-id")

    p_queue = sub.add_parser("queue", help="Queue a follow-up message on a thread via `codex queue`.")
    p_queue.add_argument("thread_id")
    p_queue.add_argument("message")
    p_queue.add_argument("--yes", action="store_true", help="Confirm queueing onto this thread.")
    p_queue.add_argument("--resume", action="store_true")

    p_int = sub.add_parser("interrupt", help="Interrupt an active turn.")
    p_int.add_argument("thread_id")
    p_int.add_argument("--turn-id")
    p_int.add_argument("--yes", action="store_true", help="Confirm interrupting this thread.")

    p_rev = sub.add_parser("review", help="Start a native Codex code review.")
    p_rev.add_argument("--cwd")
    p_rev.add_argument("--target", default="uncommittedChanges", choices=["uncommittedChanges", "baseBranch", "commit"])
    p_rev.add_argument("--branch")
    p_rev.add_argument("--sha")
    p_rev.add_argument("--thread-id")
    p_rev.add_argument("--yes", action="store_true")
    p_rev.add_argument("--name")
    p_rev.add_argument("--model")
    p_rev.add_argument("--effort")
    p_rev.add_argument("--service-tier")
    p_rev.add_argument("--wait", action="store_true")
    p_rev.add_argument("--timeout", type=float, default=240.0)

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

    dispatch: Dict[str, Tuple[Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]], Dict[str, Any]]] = {
        "status": (op_status, {}),
        "list": (
            op_list_threads,
            {
                "limit": getattr(ns, "limit", 15),
                "cwd": getattr(ns, "cwd", None),
                "search_term": getattr(ns, "search", None),
                "loaded_only": getattr(ns, "loaded_only", False),
                "include_archived": getattr(ns, "archived", False),
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
                "cursor": getattr(ns, "cursor", None),
            },
        ),
        "turn": (
            op_get_turn_details,
            {
                "thread_id": getattr(ns, "thread_id", None),
                "turn_id": getattr(ns, "turn_id", None),
                "max_output_chars": getattr(ns, "max_output_chars", 8000),
            },
        ),
        "wait": (
            op_wait_turn,
            {
                "thread_id": getattr(ns, "thread_id", None),
                "turn_id": getattr(ns, "turn_id", None),
                "timeout_seconds": getattr(ns, "timeout", 180.0),
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
                "wait": getattr(ns, "wait", False),
                "timeout_seconds": getattr(ns, "timeout", 180.0),
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
                "timeout_seconds": getattr(ns, "timeout", 180.0),
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
                "name": getattr(ns, "name", None),
                "model": getattr(ns, "model", None),
                "effort": getattr(ns, "effort", None),
                "service_tier": getattr(ns, "service_tier", None),
                "wait": getattr(ns, "wait", False),
                "timeout_seconds": getattr(ns, "timeout", 240.0),
            },
        ),
    }

    fn, args = dispatch[ns.command]
    try:
        result = fn(args, conn_kw)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
