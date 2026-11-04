## What and why / 改动内容与原因

<!-- What does this PR change, and why? Link the issue: Fixes #… -->

## How it was verified / 验证方式

<!-- Commands you ran and their result; for UI changes, screenshots in both skins. -->

## Checklist / 自查清单

See [CONTRIBUTING.md](../CONTRIBUTING.md) and [AGENTS.md](../AGENTS.md).

- [ ] Tests pass: `.venv/bin/python -m pytest -q tests` (at least the tests that read the files I changed) / 测试通过
- [ ] UI changes were checked in a real browser in **both the dark and light skin**, screenshots attached (or: no UI change) / 界面改动已在深浅两套皮肤下真实浏览器验证并附截图
- [ ] Any new Chinese UI string has an English entry in `scripts/i18n_en_source.py` and `static/i18n.en.js` was regenerated / 新增中文界面文案已补英文词条
- [ ] Display, flow and role names come from `flows.json` — no hard-coded copies / 展示名取自 `flows.json`
- [ ] Colours only live in the token blocks of `static/board.css`; components use `var(--…)` / 色值只在令牌块
- [ ] Schema changes are migrations (`ALTER TABLE`), never drop-and-recreate / 表结构只用迁移
- [ ] No personal data, real project names, credentials or absolute machine paths in code, tests or docs / 无个人数据、凭据、本机绝对路径
- [ ] Long-running processes: the PR says whether the board / dispatcher must be restarted / 说明是否需重启看板或调度器
