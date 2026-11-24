# let-agy-use-codex

A **Google Antigravity (AGY)** plugin that lets AGY inspect, collaborate with, and delegate tasks to **OpenAI Codex** through the `codex app-server` daemon — with **zero third-party Python dependencies**.

When both AGY and Codex Desktop / IDE / CLI attach to the same `codex app-server` daemon (note: standalone TUI sessions configured to use an embedded server run separately), you can watch AGY-driven Codex threads live in your native Codex UI and step in whenever you want.

## Features

- **Structured JSON-RPC bridge**: Talks directly to `codex app-server` over `codex app-server proxy` (or a direct `unix://`, `wss://`, or loopback `ws://` endpoint), exposing compact turn summaries alongside on-demand full command outputs and diffs with bounded response budgets.
- **Shared live threads & human-in-the-loop**: Threads created by AGY appear immediately in Codex UIs connected to the same daemon. `codex_wait_turn` polls locally (0 LLM tokens while waiting), supports MCP `notifications/cancelled` without blocking concurrent MCP calls, pauses immediately when Codex requests approval or user input (`waitingOnApproval` / `waitingOnUserInput`), follows queued follow-up turns, and surfaces mid-turn steers via `userInterventions`.
- **Atomic hot reload**: `scripts/mcp_hot_reload.py` keeps the MCP stdio connection alive and atomically reloads `codex_bridge.py` / `codex_app_client.py` in isolated module namespaces whenever they change on disk (~50µs `stat` check, zero idle overhead, zero partial-state corruption on errors, emits `notifications/tools/list_changed` when tool schemas change).
- **Safety guardrails**: Read-only tools never submit prompts or modify thread conversations (`codex_list_threads` defaults to `use_state_db_only=true` so listing avoids rollout repair writes; `codex_list_models` may fetch the model catalog on cache miss). Existing-thread write tools require explicit `confirm_modify_thread=true`, inline reviews on existing threads additionally require `confirm_replace_active_turn=true`, and multi-step writes preserve already-created `threadId`/`turnId` on partial failure.
- **Bundled AGY skill + CLI**: Ships an MCP server (`codex_*` tools), a concise `SKILL.md` guide for fresh AGY sessions, and a standalone CLI covering the same operations.

## Requirements

- **macOS or Linux** (uses POSIX `select` over sockets and subprocess pipes)
- **Python 3.9+** (standard library only)
- **OpenAI Codex CLI** (`codex` with `app-server` support, e.g. `codex-cli >= 0.157`; native `thread/queue/add` requires a daemon supporting that RPC)
- A running local Codex daemon (started automatically when you open Codex, or via `codex app-server daemon start`)

## Install in Antigravity (AGY)

### Option A: Symlink for local development

```bash
mkdir -p ~/.gemini/config/plugins
ln -sfn /path/to/let-agy-use-codex ~/.gemini/config/plugins/let-agy-use-codex
agy plugin list
```

### Option B: Install directly with `agy`

```bash
agy plugin install /path/to/let-agy-use-codex
```

Validate the plugin anytime with:

```bash
agy plugin validate --strict /path/to/let-agy-use-codex
```

Open a new AGY session (or run `/mcp` to reload MCP servers in an existing CLI session). The `codex_*` MCP tools and `let-agy-use-codex` skill load automatically. `mcp_config.json` sets `timeoutSeconds: 660` so long `codex_wait_turn` calls (up to 600s) do not hit AGY's default 180s MCP tool timeout.

### Optional: Configure per-tool permissions in AGY

AGY matches MCP permission rules by server/tool name (`Deny > Ask > Allow`).

To pre-approve **only the 7 read-only tools** while keeping all write/interactive tools prompting, add these rules to your AGY `allowlist` (`/permissions` or **Settings → Permissions**):

```text
mcp(let-agy-use-codex_codex/codex_status)
mcp(let-agy-use-codex_codex/codex_list_threads)
mcp(let-agy-use-codex_codex/codex_read_thread)
mcp(let-agy-use-codex_codex/codex_list_turns)
mcp(let-agy-use-codex_codex/codex_get_turn_details)
mcp(let-agy-use-codex_codex/codex_wait_turn)
mcp(let-agy-use-codex_codex/codex_list_models)
```

Alternatively, to allow **all** tools from this plugin (including writes) in one rule:

```text
mcp(let-agy-use-codex_codex/*)
```

And if you use the wildcard above but still want specific write actions to prompt, add those tools (such as `mcp(let-agy-use-codex_codex/codex_interrupt_turn)`) to your `asklist`.

## Included MCP Tools

| Category | Tools |
|---|---|
| **Read-only** | `codex_status`, `codex_list_threads`, `codex_read_thread`, `codex_list_turns`, `codex_get_turn_details`, `codex_wait_turn`, `codex_list_models` |
| **Create / Fork / Review** | `codex_start_thread`, `codex_fork_thread`, `codex_start_review` |
| **Interactive (guarded)** | `codex_send_turn`, `codex_steer_turn`, `codex_queue_message`, `codex_interrupt_turn` |

New threads inherit your local `~/.codex/config.toml` defaults (model, reasoning effort, service tier, sandbox, approval policy, and approvals reviewer) unless explicitly overridden per call.

## Standalone CLI Usage

The bridge script can also be invoked directly from the command line:

```bash
python3 scripts/codex_bridge.py status
python3 scripts/codex_bridge.py list --limit 10
python3 scripts/codex_bridge.py read <thread_id> --recent-turns 1
python3 scripts/codex_bridge.py turn <thread_id> <turn_id> --limit 100
python3 scripts/codex_bridge.py start --cwd /path/to/repo --name "Review" --prompt "..." --sandbox read-only --wait
```

## Environment Variables (Optional)

- `CODEX_BIN`: Explicit path to the `codex` binary (auto-discovers `/opt/homebrew/bin/codex`, `/usr/local/bin/codex`, `~/.local/bin/codex`, etc.).
- `CODEX_APP_SERVER_SOCK`: Custom Unix control socket path for `codex app-server proxy --sock`.
- `CODEX_REMOTE_URL`: Connect directly to `wss://host:port`, `ws://127.0.0.1:port`, or `unix:///path/to.sock` instead of `codex app-server proxy`.
- `CODEX_REMOTE_AUTH_TOKEN`: Optional bearer token for remote connections. For safety, bearer tokens are refused over plaintext `ws://` to non-loopback hosts; use `wss://` (with TLS certificate and hostname verification) or loopback `ws://`.

## Tests

```bash
python3 -m unittest discover -s tests -v
# Optional read-only smoke test against a running local Codex daemon:
python3 tests/live_smoke_readonly.py
```

## AI Disclosure

This project (code, tests, skill instructions, and documentation) was written by **Gemini 4 Neon** running inside Google Antigravity, under human direction and review.

## License

MIT — see [LICENSE](./LICENSE).
