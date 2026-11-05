<div align="center">

# Loop Engine

**Cheap models, strict gates, steady output.**
A kanban board that turns requirement cards into reviewed, tested, merged code — without you watching.

[English](README.md) · [简体中文](README.zh-CN.md)

[![CI](https://github.com/RonSpeace/loop-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/RonSpeace/loop-engine/actions/workflows/ci.yml)
![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg)

</div>

<!-- SCREENSHOT: board overview from a real project (to be added) -->

---

Loop Engine is a chat-driven loop: **requirement → development → review → acceptance → merge**.
You describe what you want in a chat, it becomes a card, and a pipeline of agents takes it from there.
Each card gets its own git worktree. Nothing merges until every gate passes.

> Loop Engine is built with Loop Engine. Most of this repository was planned, implemented, reviewed
> and merged by its own pipeline.

## Why

A single coding agent grades its own homework. A strong model makes that less risky, but it costs a lot
and still fails quietly. Loop Engine takes the other route:

- **Gates, not trust.** Separate read-only reviewers check the design and the diff, and they can reject.
  Rejected work is redone automatically. An acceptance role runs the real thing on its own test instance.
- **Fail-closed.** A missing verdict, a bad config or a failed exit check stops the card. It never
  merges half-done work; it waits for you.
- **Cheap models are enough.** Because every step is checked, an inexpensive model can do most of the
  work. After repeated rejections the engine can switch the author to a stronger model, and then hand
  the card to you.
- **Unattended.** The dispatcher picks up cards, runs the flow, merges and notifies you. You only step in
  when the inbox says so.
- **Fits your project.** Flows, roles and prompts are config. Each repository adds its own rules, guides
  and skills.

## How it works in 30 seconds

[![Loop Engine application architecture](docs/diagrams/app-architecture.svg)](docs/diagrams/app-architecture.svg)

1. **Board** (`board.py`, Flask + SQLite): cards, chat, live sessions, settings. Local only, port 8899.
2. **Dispatcher** (`dispatcher.py`): picks up `open` cards every 15 seconds, creates a worktree, runs the
   card's flow node by node, checks results, merges and sends notifications.
3. **ACP client** (`acp_shared.py`): starts each agent as a subprocess over the
   [Agent Client Protocol](https://agentclientprotocol.com) and approves every tool call according to the role.
4. **Context compiler**: builds each prompt from layers — engine rules, role template, project
   conventions, skills, then the card itself.

One card, from chat to merge:

[![Call chain of one card](docs/diagrams/call-chain.svg)](docs/diagrams/call-chain.svg)

Click a picture to open it full size. Interactive versions (zoom, search, light/dark) are in [`docs/diagrams/`](docs/diagrams/) — open the `.html` files locally, or the Diagrams tab of the in-app guide.

## Quick start

You need Python 3.11+, git and one agent runtime (see [Agent runtimes](#agent-runtimes)).

```bash
git clone https://github.com/RonSpeace/loop-engine.git && cd loop-engine
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp projects.example.json projects.json      # point it at a git repo for the agents
.venv/bin/python scripts/doctor.py           # environment self-check
./scripts/run_local.sh                       # board + dispatcher; Ctrl-C stops both
```

Open <http://127.0.0.1:8899> and create a card.

The full walkthrough — models and API keys, MCP tools, a toy project for your first loop and
troubleshooting — is in **[docs/quickstart.en.md](docs/quickstart.en.md)**.

**Docker:**

```bash
mkdir -p config && cp projects.example.json config/projects.json   # paths = paths under LOOP_REPOS
LOOP_UID=$(id -u) LOOP_GID=$(id -g) LOOP_REPOS=$HOME/code docker compose up --build
```

One container runs both processes; the database lives in a named volume and the board is published on
`127.0.0.1:8899` only. Uncomment the volume for your runtime's login (`~/.kimi-code-deepseek`, `~/.claude`, `~/.codex`).

**Linux:** use `scripts/run_local.sh` (launchd and `start.sh` are macOS-only). Logs go to
`~/.local/state/loop-engine/logs` (override with `REQ_BOARD_LOG_DIR`). For a long-running service, wrap the
script in a systemd user unit or use Docker.

> **Security:** the board has **no authentication**. Anyone who can reach it can edit cards and make
> agents change your code. It listens on `127.0.0.1` by default. Never expose it on `0.0.0.0` or an
> untrusted network.

## Core concepts

| Concept | What it means |
|---|---|
| **Card** | One requirement. Status: `later` (deferred) → `open` (queued) → `doing` → `done`, or `dropped`. Only `open` cards are picked up. |
| **Flow** | The node sequence a card follows. `quick`: dev → finish. `standard`: dev → code review → accept → finish. `heavy`: tech design → design review → dev → code review → accept → finish. An analysis step picks the flow. |
| **Role** | One agent job: `analysis`, `tech`, `tech_review`, `dev`, `code_review`, `accept`, plus `merge_resolve` for merge conflicts. Reviewers are read-only; only `dev` and `merge_resolve` change code. |
| **Gate** | A check that can stop the card: per-role tool approval, review verdicts (`pass` / `reject`), the exit check (code changed or artifact written), and questions for you (`need_confirm`). |
| **Runtime** | The agent CLI that runs a role: `kimi`, `claude-code`, `codex` or `opencode`. Chosen per project and per role. |

Flows and roles live in `flows.json` and `prompts/*.md`. Edit them like any other config.

## Using the board

- **Create a card.** Click **+ New** (or press <kbd>N</kbd>) and describe the requirement in chat. The agent
  drafts a title and notes; you confirm. If chat is unavailable, use the **Fill in a form** fallback.
  New cards start as `later`; switch them to `open` to start work.
- **Read the progress bar.** Each running card shows a thin bar split into its flow steps. The current step
  pulses. **Orange always means "you need to decide".**
- **Clear your inbox.** **Needs you** in the header lists every card waiting on you: a question, a review
  that keeps failing, an exit check that needs a ruling, a blocked merge. Click a row to open the card.
- **Watch and steer.** Open a session to see messages, tool calls and diffs live. Type in its chat box to
  give the agent more instructions.
- **Check what the agent saw.** The card's **Context** tab shows the exact prompt layers each run received.
- **Status badge.** The header badge only tells you whether the engine is running. Feishu/Lark is an
  optional channel and does not turn it red.
- **Phone.** On a narrow screen the board switches to a single column.

Keyboard shortcuts (outside text fields):

| Key | Action |
|---|---|
| <kbd>N</kbd> | New card |
| <kbd>/</kbd> | Search |
| <kbd>J</kbd> / <kbd>K</kbd> | Next / previous card |
| <kbd>Enter</kbd> | Open the focused card |
| <kbd>Esc</kbd> | Close the panel |
| <kbd>?</kbd> | Show shortcuts |

Settings (gear icon, bottom left): **Overview**, **Models**, **Notifications** (Feishu/Lark, Webhook,
Slack), **Prompts** (override the engine's prompt text on this machine) and **Skills** (the skill library).
The animated background is off by default; turn it on under **Overview → Appearance**.

The in-app **Guide** (icon in the header) covers every screen in detail.

## Agent runtimes

Each role runs on an ACP agent runtime, chosen per project and per role:

| Runtime | Pipeline roles | Chat & assistant | Notes |
|---|---|---|---|
| `kimi` (default) | all | yes | kimi-code CLI (`kimi acp`); point it at any OpenAI / Anthropic / Gemini-compatible model |
| `opencode` | all | yes | [OpenCode](https://opencode.ai) (`opencode acp`). Works with **any OpenAI-compatible API** (DeepSeek, Qwen, GLM, your gateway): set `LOOP_OPENAI_BASE_URL`, `LOOP_OPENAI_API_KEY`, `LOOP_OPENCODE_MODEL` |
| `claude-code` | dev only | no | via `@zed-industries/claude-code-acp`; uses your logged-in `claude` CLI |
| `codex` | dev only | no | via `@zed-industries/codex-acp`; uses your logged-in `codex` CLI |

```json
{"my-project": {"paths": ["/path/to/repo"], "runtimes": {"dev": "claude-code"}}}
```

Selection order: `projects.json` `<project>.runtimes.<role>` → `<project>.runtime` → `flows.json`
`roles.<role>.runtime` → `LOOP_AGENT_RUNTIME` → `kimi`. Unknown runtimes are refused before anything starts.

Read-only roles, chat and the board assistant only run on runtimes that pass the **runtime certification
suite** ([`tests/test_runtime_certification.py`](tests/test_runtime_certification.py)). It drives the real
runtime with a scripted model that tries to write files, run commands, read outside the project and spawn
sub-agents; every attempt must be stopped. Kimi and OpenCode pass. Claude Code and Codex stay dev-only
until they do. If the configured runtime cannot start a chat, the board tells you what to change.

Setup details: [quickstart → Agent runtimes](docs/quickstart.en.md#agent-runtimes).

## Documentation

| Read this | For |
|---|---|
| [Quick start](docs/quickstart.en.md) | Install, models, first loop, Docker, troubleshooting |
| In-app guide (`/guide`, source [docs/guide.en.html](docs/guide.en.html)) | Using the board day to day |
| [Project manifest & prompt layers](docs/project-manifest.md) | Teaching agents your project's rules (`.loop/`) |
| [Dispatcher internals](docs/dispatcher.md) | Scheduling, gates, merging, notifications |
| [REST API](docs/api.md) · [Data model](docs/data-model.md) | Integrating with the board |
| [Diagrams](docs/diagrams/) | Interactive architecture and call-chain diagrams (sources in `src/`) |
| [AGENTS.md](AGENTS.md) | Contributor rules — the same rules the agents follow |

Some internal docs are in Chinese.

## Contributing

Issues and pull requests are welcome; please read [CONTRIBUTING.md](CONTRIBUTING.md) first.

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q tests
```

Roadmap:

- [x] Pluggable runtimes: OpenCode for every role, Claude Code and Codex for dev
- [x] English UI · Docker Compose
- [ ] Claude Code / Codex for reviewer roles, Gemini CLI
- [ ] English prompt set
- [ ] One-command install
- [ ] GitHub Issues ⇄ cards sync, PR mode
- [ ] Shareable card replays

## License

[MIT](LICENSE)
