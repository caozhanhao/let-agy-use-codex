---
name: let-agy-use-codex
description: >-
  Use this skill whenever the user asks Antigravity (AGY) to inspect, watch,
  collaborate with, delegate tasks to, or request code reviews from local
  OpenAI Codex sessions via the bundled `codex_*` MCP tools (backed by the
  local Codex app-server daemon).
---

# Let AGY Use Codex

This plugin connects Antigravity directly to the user's running **OpenAI Codex `app-server` daemon** via native `codex_*` MCP tools. Because both AGY and Codex Desktop / VS Code / CLI attach to the same daemon, any thread created or updated here is **live-visible in the user's Codex UI** in real time.

## 1. Safety Guardrails (Always Follow)

1. **Read-only by default on user threads**: Never call `codex_send_turn`, `codex_steer_turn`, `codex_queue_message`, `codex_interrupt_turn`, or `codex_start_review(thread_id=...)` on a thread the user already owns unless the user **explicitly** asks you to send/steer/interrupt that thread.
2. **Isolate AGY-initiated work**:
   - For a new delegated task or second opinion -> call `codex_start_thread` (give it a clear `name` so the user can spot and watch it in Codex UI).
   - To build on an existing user thread's context without disturbing it -> call `codex_fork_thread`, then work on the forked thread.
   - For a code review -> call `codex_start_review` without `thread_id` (it automatically creates a clean read-only review thread).
3. **Respect human-in-the-loop approvals**: When `codex_wait_turn` returns `waitOutcome: "waiting_for_user"` (with `activeFlags` like `waitingOnApproval` or `waitingOnUserInput`), stop waiting immediately and tell the user that Codex is waiting for their approval or reply in the Codex UI.

## 2. Quick Tool Map

### Read-Only (Always Safe)
- `codex_status` — Verify daemon connectivity and list currently loaded thread IDs.
- `codex_list_threads` — Browse threads (`loaded_only=true`, `cwd=...`, `search_term=...`, `limit=15`).
- `codex_read_thread` — Inspect one thread's metadata, live `status`/`activeFlags`, and latest turn(s).
- `codex_list_turns` — Paginate older turns (`sort_direction="desc"`, `limit=5`, `cursor=...`).
- `codex_get_turn_details` — Fetch full command outputs and file diffs for **one specific** `turn_id`.
- `codex_wait_turn` — Block locally (0 LLM polling tokens) until an active turn finishes or needs human approval.
- `codex_list_models` — Check available Codex models, reasoning efforts, and service tiers (e.g. `fast`).

### Write / Interactive
- `codex_start_thread` — Create a new thread on the shared daemon (optionally with `name`, `prompt`, `model`, `effort`, `service_tier`, `sandbox`, `wait=true`).
- `codex_fork_thread` — Fork an existing thread into a new isolated copy (optional `service_tier` override).
- `codex_send_turn` — Send a prompt to an idle thread (requires `confirm_modify_thread=true`; supports `service_tier` and single-turn `service_tier_for_turn`).
- `codex_steer_turn` — Mid-flight course-correction on an `inProgress` turn (requires `confirm_modify_thread=true`).
- `codex_queue_message` — Queue a follow-up via `codex queue` without interrupting (requires `confirm_modify_thread=true`).
- `codex_interrupt_turn` — Stop an `inProgress` turn (requires `confirm_modify_thread=true`).
- `codex_start_review` — Run Codex's native code review (`uncommittedChanges`, `baseBranch`, or `commit`).

## 3. Token-Efficient Usage Patterns

- **Inspecting a thread**: Start with `codex_read_thread(thread_id=..., recent_turns=1)` (defaults to `items_view="compact"`, which preserves **all** user messages including mid-turn steers while keeping heavy stdout/diffs compacted). Only call `codex_get_turn_details(thread_id, turn_id)` when you actually need full command stdout or full unified diffs.
- **Handling human mid-turn steers**: By default, `codex_wait_turn` stays quiet while the user steers Codex directly in the Codex UI, follows any queued/follow-up turn until Codex settles (`follow_new_turns=true`), and surfaces every mid-turn steer / follow-up message in `userInterventions` (and `turn.midTurnSteers`). Always read `userInterventions` when `codex_wait_turn` returns so your understanding stays aligned with what the user told Codex mid-flight. (Pass `return_on_user_steer=true` only if you explicitly want to wake up mid-turn.)
- **Delegating a task while the user watches**:
  1. **New thread vs reuse**: Open a new thread (`codex_start_thread`) per independent milestone, module, or review; reuse the same thread (`codex_send_turn`) for follow-up fixes or test iterations within the same milestone.
  2. Call `codex_start_thread(cwd=..., name="AGY: <short task>", prompt=..., wait=false)` (inherits `~/.codex/config.toml` defaults for model, effort, service tier, sandbox, and approvals unless you explicitly override `model`, `effort`, `service_tier="fast"`, or `sandbox="read-only"`).
  3. **Immediately tell the user** the new thread's `name`, `threadId`, and `resumeCommand` (`codex resume <id>`) so they know a new Codex session was opened and can watch live.
  4. **Waiting strategy**:
     - **Short tasks (< ~1–2 min)**: use `wait=true` or `codex_wait_turn`.
     - **Long tasks (> ~2 min, e.g. milestones / big refactors / deep reviews)**: prefer `wait=false`, return control to the user right away, and periodically check progress with `codex_read_thread` (for example using AGY's `schedule` timer) instead of blocking the foreground UI on a long `codex_wait_turn`.
  5. If `codex_wait_turn` returns `waitOutcome == "waiting_for_user"`, notify the user to handle the approval or question in Codex UI.
- **When `codex_status` fails to connect**: Tell the user the local Codex app-server daemon is not running yet and suggest starting Codex or running `codex app-server daemon start`.
