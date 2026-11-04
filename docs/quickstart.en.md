# Quick start

English · [简体中文](quickstart.md)

This guide gets Loop Engine running on a fresh machine and walks one toy requirement through the full loop:
card → analysis → development → code review → acceptance → automatic merge.

- macOS and Linux are supported. Foreground mode (`scripts/run_local.sh`) works on both; launchd supervision is macOS only.
- There is no hot reload for configuration or prompts: restart both the board and the dispatcher after changing them.
- To share the engine code between two machines while keeping each machine's runtime data, see [two-machine-sync.md](two-machine-sync.md) (Chinese).

## 0. What you need

```
your machine
├─ Python 3.11+ (a .venv is recommended)  ← board.py (the board) and dispatcher.py (the dispatcher)
├─ git                                     ← worktrees, merging, the toy project
├─ kimi-code CLI (the `kimi` command)      ← default agent runtime; looked up as $KIMI_BIN → PATH → ~/.kimi-code/bin/kimi
├─ harness home (default ~/.kimi-code-deepseek, override with KIMI_CODE_HOME)
│    ├─ config.toml                        ← model providers, API keys, model aliases
│    └─ mcp.json                           ← MCP tools (playwright for browser checks)
└─ this repository
     ├─ .env                               ← optional local environment variables (template: .env.example)
     ├─ projects.json                      ← the projects agents will work on (template: projects.example.json)
     ├─ feishu.json                        ← optional Feishu/Lark bot credentials
     └─ data/board.db                      ← SQLite, tables created on first start
Artifacts and worktrees are created under ~/.req-board/{artifacts,worktrees}/
```

## 1. Install

```bash
git clone https://github.com/RonSpeace/loop-engine.git && cd loop-engine
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# optional: Feishu/Lark bot
# .venv/bin/pip install -r requirements-feishu.txt
# optional: read-only MySQL queries for a project
# .venv/bin/pip install mysql-connector-python
```

Install the kimi-code CLI and check it works:

```bash
kimi --version          # or ~/.kimi-code/bin/kimi --version
```

If your kimi build needs a login or activation, run `kimi` interactively once first.

## 2. Models and API keys

Every background agent is started through `scripts/kimi-chat-acp`, which points `KIMI_CODE_HOME` at
`~/.kimi-code-deepseek` by default (your interactive `~/.kimi-code` is left alone). Model and MCP configuration
both live in that directory.

### 2.1 config.toml

```bash
mkdir -p ~/.kimi-code-deepseek
# if you already have a working ~/.kimi-code setup, start from a copy:
# cp ~/.kimi-code/config.toml ~/.kimi-code-deepseek/config.toml
```

`~/.kimi-code-deepseek/config.toml` looks roughly like this (check the docs of your kimi-code version for exact fields):

```toml
default_model = "deepseek/v4.1-flash"

[providers.deepseek]
type = "openai"                       # openai / anthropic / openai_responses / google-genai / kimi
base_url = "https://api.deepseek.com/v1"
api_key = "sk-your-key"               # the API key lives here only, never in this repository

[models."deepseek/v4.1-flash"]
provider = "deepseek"
model = "deepseek-flash"              # the provider's model name
max_context_size = 128000
```

Once the file exists you can also edit providers, keys and aliases from the board under Settings → Models.

### 2.2 Keep model names consistent

The global model comes from the `KIMI_MODEL` environment variable (default `deepseek/v4.1-flash`) and must be an
alias that exists in `config.toml`. To change it, put it in `.env` (`run_local.sh` exports it):

```bash
KIMI_MODEL=deepseek/v4.1-flash
```

Each role can use a different model. The first value found wins:

1. `projects.json` `<project>.models.<role>` (the board's "Model tuning" panel at the right end of the project bar; applies from the next card);
2. `flows.json` `roles.<role>.model`;
3. environment variable `KIMI_MODEL_<ROLE>`, e.g. `KIMI_MODEL_DEV`;
4. global `KIMI_MODEL`.

For example, to use a fast model for analysis and review and a strong one for development: define two aliases in
`config.toml`, set `roles.dev.model` in `flows.json` to the strong alias, and restart the board and dispatcher.
The effective model of each role is shown on the board's `/guide` page under "Built-in agents".

### 2.3 mcp.json

`~/.kimi-code-deepseek/mcp.json` needs at least `playwright`, which the dev and accept roles use for browser-level
checks. The server must be named exactly `playwright`:

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest"]
    }
  }
}
```

If you already have `~/.kimi-code/mcp.json`, you can symlink it: `ln -s ~/.kimi-code/mcp.json ~/.kimi-code-deepseek/mcp.json`.
The read-only research tools (reading files, searching, git log, …) are built in and need no configuration.

### 2.4 Smoke test

```bash
KIMI_CODE_HOME=~/.kimi-code-deepseek kimi --version
KIMI_CODE_HOME=~/.kimi-code-deepseek kimi      # optional: ask something to confirm the key and model work
```

## Agent runtimes

Each role drives an agent runtime over ACP. The registry is `acp_shared.AGENT_RUNTIMES`:

| Runtime | Roles | Notes |
|:--|:--|:--|
| `kimi` (default) | all | kimi-code CLI, models configured as in section 2 |
| `claude-code` | dev only | uses `claude-code-acp` from PATH, otherwise `npx -y @zed-industries/claude-code-acp@0.16.2`; log in to the `claude` CLI first |
| `codex` | dev only | uses `codex-acp` from PATH, otherwise `npx -y @zed-industries/codex-acp@0.16.0`; log in to the `codex` CLI first |

The last two need Node.js / npm. Selection order: `projects.json` `<project>.runtimes.<role>` → `<project>.runtime` →
`flows.json` `roles.<role>.runtime` → environment variable `LOOP_AGENT_RUNTIME` → `kimi`. Unknown or empty runtime ids are
refused before anything starts.

```json
{"my-project": {"paths": ["/path/to/repo"], "runtimes": {"dev": "codex"}}}
```

- Claude Code and Codex use their adapters' default models; kimi model aliases do not apply to them.
- They are **limited to the dev role** for now: it cannot yet be proven that the read-only limits hold on those runtimes,
  so analysis, review, acceptance, conflict resolution and assistant sessions refuse to start on them. Keep those roles
  on `kimi`; switching the global runtime would stall those stages.
- To override the launch command, set `LOOP_AGENT_CMD_KIMI`, `LOOP_AGENT_CMD_CLAUDE_CODE` or `LOOP_AGENT_CMD_CODEX`.
  The value is split into arguments using shell quoting rules (it is not run through a shell; for kimi include `acp`
  yourself), e.g. `LOOP_AGENT_CMD_CODEX='/opt/bin/codex-acp'`.
- Restart the board and dispatcher after editing `.env` or `flows.json`; a project-level choice applies from the next new session.

## 3. Feishu / Lark (optional)

**Without Feishu** (recommended to start): every confirmation happens on the board, nothing is missing. The dispatcher
turns Feishu off automatically when `lark-oapi` is not installed or no credentials exist; you can also set
`REQ_BOARD_NO_FEISHU=1` explicitly.

**With your own Feishu bot**:

1. On the Feishu open platform, create a custom app, enable the bot capability and choose **long connection (WebSocket)**
   for event subscription (no public URL needed).
2. Request permissions to send and receive direct messages (`im:message` and related), then publish a version.
3. On the board, open Settings → Notifications, and in the Feishu bot section enter the App ID / App Secret and save (written to `feishu.json` at the repo root).
4. Restart the dispatcher (it reads credentials only at start-up), then send the bot a direct message to open the conversation.

## 4. Prepare a project

### 4.1 Create a toy project

This script creates a git repository with code and tests at `~/projects/toy-project` (use `TOY_DIR=...` to change it):

```bash
bash scripts/setup_toy_project.sh
```

To do it by hand: a git repository with at least one commit, a little code with tests (so dev has work to do and
accept has something to verify), and an `AGENTS.md`.

### 4.2 The project's AGENTS.md

`AGENTS.md` holds the project's own rules (build/test commands, code style, layout) plus the board collaboration rules:

```markdown
## Requirements board conventions

This project's requirements board runs at http://127.0.0.1:8899 (skip silently if it is not running).

1. Deferred requirements: POST /api/requirements with project = this directory name and status "later".
2. When you start work on a card, PATCH its status to doing.
3. When done, match the card by title and PATCH it to done; if unsure which card, ask the user first.
4. Send the header X-Actor: agent:toy-project:<card id> when changing a status.
5. Notes describe only what to build, not the discussion.
```

### 4.3 Register it in projects.json

```bash
cp projects.example.json projects.json
# set toy-project's paths to your absolute path
```

Alternatively, once the board is running, click `+` next to "Projects" in the left sidebar and fill in the name,
repository paths, main branch and goal — no restart needed. `projects.json` is local data and is gitignored. Optional
fields (`branch_only`, `git_sync`, `restart`, `budgets`, `models`, `runtimes`, …) can all be left out for now; they are
described in [dispatcher.md](dispatcher.md) (Chinese).

### 4.4 Trust the working directory

kimi keeps a per-directory trust list in the harness home. Run it once interactively in the project directory with
the same home and accept:

```bash
cd ~/projects/toy-project && KIMI_CODE_HOME=~/.kimi-code-deepseek kimi
```

If an agent later reports an untrusted directory under `~/.req-board/worktrees/`, accept that directory the same way.

## 5. Start

Run the self-check first:

```bash
.venv/bin/python scripts/doctor.py
```

It passes when it exits with code 0 and reports 0 hard failures (「硬失败 0 项」). ⚠️ lines are optional items.

**Foreground** (macOS and Linux; Ctrl-C stops everything):

```bash
./scripts/run_local.sh               # board + dispatcher
# or separately: ./scripts/run_local.sh board / ./scripts/run_local.sh dispatcher
```

**Always-on on macOS** (launchd: starts at login, restarts on crash):

```bash
./scripts/install-launchd.sh         # once after install, after moving machines, or after editing plists/*.tmpl
./start.sh                           # start / restart the board
```

If the repository is under `~/Documents`, grant `/bin/bash` Full Disk Access (System Settings → Privacy & Security),
otherwise launchd cannot read it. Logs go to `~/Library/Logs/req-board/`. On Linux, supervise `scripts/run_local.sh`
with systemd or similar.

**Check that it is running**:

| Check | How | Expected |
|:--|:--|:--|
| Board | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8899` | `200` |
| Project registered | `curl -s http://127.0.0.1:8899/api/auto-projects` | `["toy-project"]` |
| Dispatcher heartbeat | `last_poll_at` in `data/dispatcher_heartbeat.json` | updates about every 15 s |
| Page | open http://127.0.0.1:8899 | the health badge at the top right is not red |

## 6. Your first loop

1. Create an **open** card with clear acceptance criteria — via chat on the board (then set the status to Open),
   or directly through the API:

   ```bash
   curl -s -X POST http://127.0.0.1:8899/api/requirements \
     -H 'Content-Type: application/json' \
     -d '{"project": "toy-project", "status": "open",
          "title": "Add a mul function to calc with tests",
          "notes": "Acceptance: mul(2,3)=6; cover negatives and 0; all unit tests pass."}'
   ```

2. After about a minute without edits the dispatcher picks the card up and it turns In progress, moving through:
   - **Analysis**: writes `analysis.md` and recommends a flow (standard, for this card);
   - **Development**: creates a worktree (branch `f-YYYYMMDD-<id>`), changes code, tests, commits;
   - **Code review**: read-only review of the diff, passes it on;
   - **Acceptance**: verifies independently, writes `accept.md`;
   - **Finalize**: merges into toy-project's main with `--no-ff`, marks the card Done, removes the worktree and branch.
3. Watch the card's timeline panel (live events) and `~/.req-board/artifacts/toy-project/req-<id>/` (each role's artifacts).
4. Afterwards `git -C ~/projects/toy-project log --oneline` shows an `auto merge req #<id>` merge commit.

When an agent asks a question (need_confirm), answer with the buttons on the card or in the chat box on the right.
If something stalls, read the last events in the timeline and the dispatcher log. A failed agent process is retried
once; if it fails again the card is deferred with the reason in its summary — fix it and set the card back to open.

## 7. Common settings

| Setting | Where |
|:--|:--|
| Projects and repositories | `projects.json` (or `+` in the board sidebar) |
| Models and API keys | `config.toml` in the harness home + `KIMI_MODEL` |
| Per-role / per-project models | `flows.json` `roles.<role>.model`; per project in the board's "Model tuning" |
| Agent runtime | `projects.json` `runtimes` / `runtime`, or `flows.json` `roles.<role>.runtime` |
| kimi binary | `KIMI_BIN` |
| Harness home | `KIMI_CODE_HOME` (default `~/.kimi-code-deepseek`) |
| Board port / address | `REQ_BOARD_PORT` (default 8899); the dispatcher reaches the board via `REQ_BOARD_API` |
| Storage time zone | `REQ_BOARD_TZ` (must match for both processes) |
| Feishu | `feishu.json` (Settings drawer); `REQ_BOARD_NO_FEISHU=1` turns it off |
| Per-project conventions | `.loop/project.json` in each repository — see [project-manifest.md](project-manifest.md) (Chinese) |

`.env.example` lists every optional environment variable.

## 8. Troubleshooting

| Symptom | Fix |
|:--|:--|
| kimi not found | Install the kimi-code CLI, or `export KIMI_BIN=/path/to/kimi` |
| Model errors / 401 in sessions | Check the key and model names in `config.toml` and align `KIMI_MODEL`; smoke-test as in 2.4 |
| Agent reports an untrusted directory | Run `KIMI_CODE_HOME=~/.kimi-code-deepseek kimi` in that directory and accept once |
| A card is never picked up | Status is open, unedited for ≥ 1 minute, and its project name matches a `projects.json` key exactly |
| Worktree creation fails | The project path must be a git repository with commits on its main branch; set `base_branch` if needed |
| Browser checks report no tools | `mcp.json` has no server named `playwright` |
| Feishu shows "Disabled" | `lark-oapi` is not installed or `REQ_BOARD_NO_FEISHU=1` is set |
| No Feishu messages | Check credentials, that the app is published and uses long connection; restart the dispatcher after changing credentials |
| Changes to `flows.json` / `prompts/` have no effect | Restart: in foreground mode Ctrl-C and rerun `run_local.sh`; under launchd `./start.sh` and `scripts/dispatcher-restart.sh <pid>` |
| Port conflict | Set `REQ_BOARD_PORT` and update the dispatcher's `REQ_BOARD_API` to match |
| launchd services fail (`Operation not permitted`) | Grant `/bin/bash` Full Disk Access, then rerun `scripts/install-launchd.sh` |
| Need an isolated test board | `scripts/test_instance.sh start <port> --seed`, clean up with `scripts/test_instance.sh stop <port>` |

## 9. Next steps

- Connect a real project: register it as in section 4 and add a `.loop/project.json` to its repository (test commands,
  guides, high-risk paths). See the "Project setup" tab on the board's `/guide` page and [project-manifest.md](project-manifest.md).
- How flows and statuses work: the board's `/guide` page (switch to English with the link at the top).
- Dispatcher internals: [dispatcher.md](dispatcher.md); REST API: [api.md](api.md) (both Chinese).
