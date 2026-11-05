#!/usr/bin/env python3
"""注入预览（context-design 第八节）：用调度器同一个上下文编译器做 dry 编译，输出 JSON。

看板 GET /api/context/preview 以子进程运行本脚本（隔离调度器模块状态，超时 20 秒）：
    python scripts/context_preview.py --project P --role R [--req-json -]   # 卡片 JSON 从 stdin 读
不拉会话、不建 worktree、不写 diff.patch、不改任何状态。卡片缺省时用占位卡：前置角色 → 前置阶段；
其它角色 → 放进第一条含该角色的启用流程（默认流程优先）的对应节点。
输出：与 manifest.json 同结构（无 run_key、kind="preview"），外加 "prompt"；出错输出 {"error": ...} 并退出码 2。
环境变量：REQ_BOARD_PROJECTS（项目清单路径，测试实例用）、REQ_BOARD_ARTIFACTS（产物根）。
"""
import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _placeholder_req(d, project, role):
    req = {"id": 0, "project": project, "title": "（示例卡片：注入预览）",
           "notes": "（示例 notes：选择一张已有卡片可预览真实输入）", "created_at": "2026-01-01 00:00:00"}
    import acp_shared as ac
    if role == ac.pre_role():
        return req
    flows = ac.enabled_flows()
    default = ac.default_flow_id()
    flows.sort(key=lambda f: 0 if f["id"] == default else 1)
    for f in flows:
        for i, n in enumerate(f["nodes"]):
            if n.get("role") == role:
                req.update(flow=f["id"], nodes_json=json.dumps(f["nodes"], ensure_ascii=False),
                           node_index=i, mode="auto")
                return req
    return req


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--role", required=True)
    ap.add_argument("--req-json", default=None, help="'-' = 从 stdin 读卡片 JSON")
    args = ap.parse_args(argv)
    import dispatcher as d
    if os.environ.get("REQ_BOARD_PROJECTS"):
        d.PROJECTS_FILE = Path(os.environ["REQ_BOARD_PROJECTS"])
        d.PROJECTS = d.load_projects()
    if os.environ.get("REQ_BOARD_ARTIFACTS"):
        d.ARTIFACTS_ROOT = Path(os.environ["REQ_BOARD_ARTIFACTS"])
    if args.project not in d.PROJECTS:
        return _fail(f"未知项目: {args.project}")
    if not d.role_prompt(args.role):
        return _fail(f"未知角色: {args.role}")
    req = None
    if args.req_json == "-":
        raw = sys.stdin.read().strip()
        req = json.loads(raw) if raw else None
    if not req:
        req = _placeholder_req(d, args.project, args.role)
    req["project"] = args.project
    if args.role == "merge_resolve":
        d.state.setdefault("merge_resolve", {}).setdefault(
            str(req["id"]), {"failed": list(d.PROJECTS[args.project].get("paths") or [])[:1]})
    compiled = d.compile_context(req, args.role, "", dry=True)
    m = d.build_manifest(req, args.role, compiled, compiled.prompt, kind="preview")
    m.pop("run_key", None)
    m["prompt"] = compiled.prompt
    m["placeholder_card"] = not bool(req.get("id"))
    print(json.dumps(m, ensure_ascii=False))
    return 0


def _fail(msg):
    print(json.dumps({"error": msg}, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    sys.exit(main())
