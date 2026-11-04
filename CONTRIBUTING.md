# Contributing to Loop Engine

Thanks for your interest! Issues, discussions and pull requests are all welcome.
Chinese and English are both fine in issues and PRs.

## Before you start

- Read [AGENTS.md](AGENTS.md). It is the single source of project conventions — for humans and for
  the agents that develop this repository. The details behind each rule are in
  [docs/agents-detail.md](docs/agents-detail.md).
- For anything larger than a small fix, open an issue first so we can agree on the approach.

## Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python scripts/doctor.py
```

Run an isolated board instance with deterministic seed data (it never touches `data/`):

```bash
REQ_BOARD_PYTHON=.venv/bin/python scripts/test_instance.sh start 8931 --seed
# … open http://127.0.0.1:8931 …
scripts/test_instance.sh stop 8931
```

## Pull request checklist

- [ ] Tests pass: `.venv/bin/python -m pytest -q tests` (at least the tests that read the files you changed)
- [ ] UI changes were checked in a real browser, in both the dark and light skin, and the PR includes screenshots
- [ ] Display names, flow names and role names come from `flows.json` — no hard-coded copies
- [ ] Colors only live in the token blocks at the top of `static/board.css`; components use `var(--…)`
- [ ] Schema changes are migrations (`ALTER TABLE`), never drop-and-recreate
- [ ] No personal data, real project names, credentials or absolute machine paths in code, tests or docs

## Adding a role or flow

1. Add the role to `flows.json` (`roles`) and reference it from a flow's `nodes`.
2. Add its capabilities to `acp_shared.ROLE_CAPABILITIES` (approval mode, write allow-list, Bash policy).
3. Add `prompts/<role>.md`.
4. Restart the board and the dispatcher (no hot reload for flows and prompts).

## Reporting security issues

Please do not open a public issue for security problems. Use GitHub's private vulnerability
reporting on this repository instead.
