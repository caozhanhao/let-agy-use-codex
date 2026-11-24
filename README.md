# let-agy-use-codex

A **Google Antigravity (AGY)** plugin that lets AGY inspect, collaborate with, and delegate tasks to **OpenAI Codex** through the local `codex app-server` daemon — with **zero third-party Python dependencies**.

Because both AGY and Codex Desktop / IDE / CLI connect to the same local `codex app-server` daemon, you can watch AGY-driven Codex threads live in your native Codex UI and step in whenever you want.

## Features

- **Structured JSON-RPC bridge**: Talks directly to the local `codex app-server` daemon over `codex app-server proxy` (or a direct `unix://` / `ws://` endpoint), exposing compact turn summaries alongside on-demand full command outputs and diffs.
- **Shared live threads & human-in-the-loop**: Threads created by AGY appear immediately in your Codex Desktop / IDE / CLI sidebar. `codex_wait_turn` polls locally (0 LLM tokens while waiting), pauses immediately when Codex requests approval or user input (`waitingOnApproval` / `waitingOnUserInput`), follows queued follow-up turns, and surfaces any mid-turn messages you typed in Codex via `userInterventions`.
- **Automatic hot reload**: `scripts/mcp_hot_reload.py` keeps the MCP stdio connection alive and reloads `codex_bridge.py` / `codex_app_client.py` automatically on the next tool call whenever they change on disk (~50µs `stat` check, zero idle overhead, emits `notifications/tools/list_changed` when tool schemas change).
- **Safety guardrails**: Read-only tools never modify Codex state; existing-thread write tools require explicit `confirm_modify_thread=true`, and `codex_send_turn` refuses to collide with an `inProgress` turn.
- **Bundled AGY skill + CLI**: Ships both an MCP server (`codex_*` tools), a 1-page `SKILL.md` guide for fresh AGY sessions, and an equivalent standalone CLI.

## Requirements

- **macOS or Linux** (uses POSIX `select` over subprocess pipes)
- **Python 3.9+** (standard library only)
- **OpenAI Codex CLI** (`codex` with `app-server` support, e.g. `codex-cli >= 0.157`)
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

Open a new AGY session (or run `/mcp` to reload MCP servers in an existing CLI session). The `codex_*` MCP tools and `let-agy-use-codex` skill load automatically.

### Optional: Reduce per-tool approval prompts in AGY

By default, AGY prompts once per MCP tool. To allow all tools from this plugin in one rule, run `/permissions` in AGY CLI (or open **Settings → Permissions**) and add to your allowlist:

```text
mcp(let-agy-use-codex_codex/*)
```

If you prefer write actions (such as `codex_send_turn` or `codex_interrupt_turn`) to keep prompting, add those specific tools to your `asklist` (`Ask` overrides `Allow`).

## Included MCP Tools

| Category | Tools |
|---|---|
| **Read-only** | `codex_status`, `codex_list_threads`, `codex_read_thread`, `codex_list_turns`, `codex_get_turn_details`, `codex_wait_turn`, `codex_list_models` |
| **Create / Fork / Review** | `codex_start_thread`, `codex_fork_thread`, `codex_start_review` |
| **Interactive (guarded)** | `codex_send_turn`, `codex_steer_turn`, `codex_queue_message`, `codex_interrupt_turn` |

New threads inherit your local `~/.codex/config.toml` defaults (model, reasoning effort, sandbox, approval policy, and approvals reviewer) unless explicitly overridden per call.

## Standalone CLI Usage

The same bridge script can be invoked directly from the command line:

```bash
python3 scripts/codex_bridge.py status
python3 scripts/codex_bridge.py list --limit 10
python3 scripts/codex_bridge.py read <thread_id> --recent-turns 1
python3 scripts/codex_bridge.py turn <thread_id> <turn_id>
python3 scripts/codex_bridge.py start --cwd /path/to/repo --name "Review" --prompt "..." --sandbox read-only --wait
```

## Environment Variables (Optional)

- `CODEX_BIN`: Explicit path to the `codex` binary (auto-discovers `/opt/homebrew/bin/codex`, `/usr/local/bin/codex`, `~/.local/bin/codex`, etc.).
- `CODEX_APP_SERVER_SOCK`: Custom Unix control socket path for `codex app-server proxy --sock`.
- `CODEX_REMOTE_URL`: Connect directly to `ws://host:port` or `unix:///path/to.sock` instead of `codex app-server proxy`.
- `CODEX_REMOTE_AUTH_TOKEN`: Optional bearer token when using `ws://`.

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
