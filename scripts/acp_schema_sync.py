#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ACP 官方 schema 同步 / 漂移检查（离线契约测试 tests/test_acp_schema_contract.py 的配套工具）。

背景：acp_shared.ACPSession 是手写 ACP 客户端（不用 acp SDK），协议面靠约定维持。
kimi 0.37.2 新增「Bash 委托客户端执行」（terminal/*，upstream#351）就是一次实战教训：
上游新增客户端方法时，手写客户端只有真跑起来才会发现。
因此把官方 schema/v1/meta.json 逐字节 vendored 到 tests/fixtures/acp_meta_v1.json，
测试离线校验我方实际收发的 ACP 方法面；本脚本负责刷新与漂移检查（唯一的联网入口）。

用法：
    python3 scripts/acp_schema_sync.py            # 检查上游是否漂移（默认对比 main）
    python3 scripts/acp_schema_sync.py --ref schema-v1.21.0   # 对比指定 tag
    python3 scripts/acp_schema_sync.py --write    # 用上游内容覆盖 vendored 文件并打印新 sha256
    python3 scripts/acp_schema_sync.py --schema   # 额外打印完整 JSON Schema（schema.json）的 sha256 供参考

退出码：0 = 与上游一致 / 写入成功；1 = 有漂移（需人工评估）；2 = 取上游失败。
注意：漂移不一定是坏事——上游新增方法时应评估「我方是否要支持」，评估结果写进测试里的
EXPECTED_UNIMPLEMENTED / DEVIATIONS 断言，再 --write 更新 vendored 文件与下面的 sha256 常数。
"""
import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UPSTREAM_RAW = ("https://raw.githubusercontent.com/agentclientprotocol/"
                "agent-client-protocol/{ref}/schema/v1/{name}")
VENDORED = REPO / "tests" / "fixtures" / "acp_meta_v1.json"

# vendored 文件的出处与指纹（改 vendored 文件必须同步改这里，测试会核对 sha256）
VENDORED_REF = "main"
VENDORED_AT = "2026-09-16"
VENDORED_META_SHA256 = "061edb6efa8fb2aa2792459a86ec7268de5fe665bba48b2ffe7939df01481f88"


def fetch(ref, name, timeout=20):
    """取上游原始文件字节（唯一联网点）。"""
    url = UPSTREAM_RAW.format(ref=ref, name=name)
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310（固定 https 域名）
        return resp.read()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def method_delta(local_meta, remote_meta):
    """按 agentMethods / clientMethods 分组列出上游相对 vendored 的新增与删除。"""
    out = []
    for section in ("agentMethods", "clientMethods", "protocolMethods"):
        lset = set((local_meta.get(section) or {}).values())
        rset = set((remote_meta.get(section) or {}).values())
        added, removed = sorted(rset - lset), sorted(lset - rset)
        if added or removed:
            out.append((section, added, removed))
    return out


def report(local_data, remote_data, ref):
    d = method_delta(json.loads(local_data), json.loads(remote_data))
    print(f"vendored: sha256={sha256(local_data)[:16]}… ref={VENDORED_REF} at={VENDORED_AT}")
    print(f"upstream: sha256={sha256(remote_data)[:16]}… ref={ref}")
    if local_data == remote_data:
        print("✅ 一致：vendored 与上游逐字节相同，无需处理")
        return 0
    print("⚠️  漂移：上游内容与 vendored 不同")
    for section, added, removed in d:
        if added:
            print(f"   {section} 新增: {', '.join(added)}")
        if removed:
            print(f"   {section} 删除: {', '.join(removed)}")
    if not d:
        print("   （方法表无差异，可能是文档/字段级变化）")
    print("   处理：评估新增/删除的方法我方是否要支持 → 更新 tests/test_acp_schema_contract.py"
          " 的 EXPECTED_* / DEVIATIONS 断言 → 跑本脚本 --write 并同步 VENDORED_META_SHA256")
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="ACP 官方 schema 同步 / 漂移检查")
    ap.add_argument("--ref", default=VENDORED_REF, help=f"上游 git ref（默认 {VENDORED_REF}）")
    ap.add_argument("--write", action="store_true", help="用上游内容覆盖 vendored 文件")
    ap.add_argument("--schema", action="store_true", help="额外打印完整 schema.json 的 sha256")
    args = ap.parse_args(argv)

    try:
        remote = fetch(args.ref, "meta.json")
    except Exception as e:  # 网络失败：报错退出，不改任何文件
        print(f"❌ 取上游失败（{args.ref}）: {e}", file=sys.stderr)
        return 2

    if args.write:
        VENDORED.parent.mkdir(parents=True, exist_ok=True)
        VENDORED.write_bytes(remote)
        new_sha = sha256(remote)
        print(f"✅ 已写入 {VENDORED.relative_to(REPO)}（ref={args.ref}，sha256={new_sha}）")
        if new_sha != VENDORED_META_SHA256:
            print("⚠️  请同步更新 scripts/acp_schema_sync.py 的 VENDORED_META_SHA256 / REF / AT，"
                  "并跑 tests/test_acp_schema_contract.py 确认方法面断言")
        if args.schema:
            print("schema.json sha256 =", sha256(fetch(args.ref, "schema.json")))
        return 0

    code = report(VENDORED.read_bytes(), remote, args.ref)
    if args.schema:
        print("upstream schema.json sha256 =", sha256(fetch(args.ref, "schema.json")))
    return code


if __name__ == "__main__":
    sys.exit(main())
