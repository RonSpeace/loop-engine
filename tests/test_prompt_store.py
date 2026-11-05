#!/usr/bin/env python3
"""提示词存储 prompt_store：默认文本与兼容常量逐字节一致、占位符替换、校验、本机覆盖生命周期。

本机覆盖目录用每个用例私有的临时目录（REQ_BOARD_PROMPTS_LOCAL），不碰仓库 prompts.local/。
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests import _baseline  # noqa: E402

_baseline.pin()

import acp_shared as ac  # noqa: E402
import dispatcher as d  # noqa: E402
import prompt_store as ps  # noqa: E402


class LocalDirCase(unittest.TestCase):
    def setUp(self):
        self.local = Path(tempfile.mkdtemp(prefix="ps-local-"))
        self._env = mock.patch.dict(os.environ, {"REQ_BOARD_PROMPTS_LOCAL": str(self.local)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.local, ignore_errors=True)


class DefaultsByteIdentical(LocalDirCase):
    """搬迁验收：兼容常量 = prompts/ 默认文件逐字节一致；注册表引用的文件都存在。"""

    def test_constants_equal_default_files(self):
        pairs = {"system/common": d.COMMON_TAIL, "system/readonly-tools": ac.READONLY_TOOL_GUIDE,
                 "system/kill-redline": ac.KILL_REDLINE_TMPL, "system/pre-contract": d.PRE_SLOT_CONTRACT,
                 "system/resume-style": d.RESUME_STYLE_REMINDER}
        for iid, const in pairs.items():
            self.assertEqual(ps.default_text(iid), const, iid)
            self.assertEqual(ps.read(iid), const, iid)

    def test_registry_files_exist_and_defaults_validate(self):
        for it in ps.items():
            if it["kind"] == "role":
                continue
            self.assertTrue((ps.PROMPTS_DIR / it["file"]).is_file(), it["id"])
            self.assertEqual(ps.validate(it["id"], ps.default_text(it["id"])), [], it["id"])
            self.assertEqual(it["status"], "default", it["id"])

    def test_role_items_derived_from_flows(self):
        ids = {it["id"] for it in ps.items()}
        for role in ("analysis", "dev", "code_review", "merge_resolve"):
            self.assertIn(f"role/{role}", ids)
            self.assertEqual(ps.default_text(f"role/{role}"), ac.role_meta(role, "prompt"))
            self.assertEqual(ps.validate(f"role/{role}", ps.default_text(f"role/{role}")), [], role)

    def test_runtime_texts_render_same_as_before(self):
        """续跑/收尾/重试文案搬迁后渲染结果与搬迁前 f-string 一致。"""
        req = {"id": 77, "project": "req-board"}
        old_user = (f"继续之前看板需求 #77 的 dev 工作（角色 dev）。\n"
                    f"用户消息：hi {{x}}\nEXTRA\n"
                    f"{d.RESUME_STYLE_REMINDER}\n"
                    "提醒：结束前必须把结果 JSON 写入产物目录 result.json"
                    "（status: done/blocked/need_confirm，契约不变）。\n"
                    "工具调用步数有每轮上限（约 200 步），接近上限时先把结果 JSON 写入 "
                    "result.json 保底（契约不变，可覆盖为最终结果），再继续剩余工作，"
                    "避免会话被截断时无结果可采。")
        self.assertEqual(d.resume_text(req, "dev", "EXTRA", "hi {x}")[0], old_user)
        steps = d._steps_text("dev", req)
        old_auto = (f"继续之前看板需求 #77 的开发工作（角色 dev）。\nEXTRA\n"
                    f"{d.RESUME_STYLE_REMINDER}\n"
                    "若上次会话中途结束：先确认改动完好（git status 等）、产物目录无残留 result.json，"
                    "再继续验证。\n"
                    "提醒：结束前必须把结果 JSON 写入产物目录 result.json"
                    "（status: done/blocked/need_confirm，契约不变）。\n"
                    f"工具调用步数有每轮上限（{steps} 步），接近上限时先把结果 JSON 写入 "
                    "result.json 保底（契约不变，可覆盖为最终结果），再继续剩余工作，"
                    "避免会话被截断时无结果可采。")
        self.assertEqual(d.resume_text(req, "dev", "EXTRA")[0], old_auto)
        self.assertEqual(ps.render("system/wrapup", detail="X", wrapup_steps=15),
                         "已达到本轮工具步数上限（X），会话被调度器截停。立即停止新的探索与修改，"
                         "用不超过 15 步把当前进度写入 result.json：已完成且验证通过 → done；"
                         "否则 blocked，summary 写清已完成部分、未完成部分与下一步。写完即结束本轮。")
        self.assertEqual(ac.kill_redline_text(4321), ac.KILL_REDLINE_TMPL.format(pid=4321))


class RenderAndValidate(LocalDirCase):
    def test_render_only_declared_single_pass(self):
        out = ps.render("system/retry-hint", last_failure="值里有 {last_failure} 与 {pid}")
        self.assertIn("值里有 {last_failure} 与 {pid}", out)
        # 未声明的占位符原样保留
        self.assertIn("{pid}", ps.render("system/retry-hint", last_failure="{pid}"))

    def test_validate_codes(self):
        base = ps.default_text("system/kill-redline")
        self.assertEqual(ps.validate("system/kill-redline", base), [])
        codes = lambda t: {e["code"] for e in ps.validate("system/kill-redline", t)}  # noqa: E731
        self.assertIn("unknown_placeholder", codes(base + "{nope}"))
        self.assertIn("missing_required", codes(base.replace("{pid}", "123")))
        self.assertIn("locked_removed", codes(base.replace("【进程安全红线】", "")))
        self.assertIn("too_long", codes(base + "x" * ps.MAX_CHARS))
        self.assertIn("locked_removed",
                      {e["code"] for e in ps.validate("system/common", "随便写")})

    def test_validate_role_template(self):
        tmpl = ps.default_text("role/dev")
        codes = lambda t: {e["code"] for e in ps.validate("role/dev", t)}  # noqa: E731
        self.assertIn("missing_required", codes(tmpl.replace("{card_inputs}", "")))
        self.assertIn("format_error", codes(tmpl + "\n孤立的 { 花括号"))
        self.assertIn("card_placeholder_in_fixed", codes("{title}\n" + tmpl))
        self.assertIn("unknown_placeholder", codes(tmpl + "{bogus}"))

    def test_unknown_id(self):
        with self.assertRaises(KeyError):
            ps.read("system/nope")


class OverrideLifecycle(LocalDirCase):
    def test_save_status_stale_rebase_delete(self):
        iid = "fragment/auto-restart"
        self.assertEqual(ps.status(iid), "default")
        ps.save_override(iid, "改过的重启说明。")
        self.assertEqual(ps.read(iid), "改过的重启说明。")
        self.assertEqual(ps.status(iid), "override")
        self.assertEqual((self.local / "fragments" / "auto-restart.md.base").read_text(encoding="utf-8"),
                         ps.default_text(iid))
        # 默认升级 → stale（覆盖仍生效）；rebase 把 base 更新为当前默认
        (self.local / "fragments" / "auto-restart.md.base").write_text("旧默认", encoding="utf-8")
        self.assertEqual(ps.status(iid), "stale")
        self.assertEqual(ps.describe(iid)["base_text"], "旧默认")
        self.assertEqual(ps.read(iid), "改过的重启说明。")
        ps.save_override(iid, "再改一次。")
        self.assertEqual(ps.status(iid), "stale", "不带 rebase 保存不改 base")
        ps.save_override(iid, "再改一次。", rebase=True)
        self.assertEqual(ps.status(iid), "override")
        self.assertTrue(ps.delete_override(iid))
        self.assertEqual(ps.status(iid), "default")
        self.assertFalse((self.local / "fragments" / "auto-restart.md.base").exists())

    def test_save_rejects_invalid(self):
        with self.assertRaises(ValueError) as cm:
            ps.save_override("system/kill-redline", "没有红线")
        self.assertTrue(any(e["code"] == "locked_removed" for e in cm.exception.args[0]))
        self.assertFalse((self.local / "system" / "kill-redline.md").exists())

    def test_override_reread_without_restart(self):
        """改覆盖文件即生效（mtime 缓存失效）；角色模板覆盖经 acp_shared.role_prompt 生效。"""
        iid = "system/retry-hint"
        ps.save_override(iid, "A {last_failure}")
        self.assertEqual(ps.render(iid, last_failure="x"), "A x")
        p = self.local / "system" / "retry-hint.md"
        p.write_text("BB {last_failure}", encoding="utf-8")
        os.utime(p, ns=(p.stat().st_mtime_ns + 10_000_000, p.stat().st_mtime_ns + 10_000_000))
        self.assertEqual(ps.render(iid, last_failure="y"), "BB y")
        self.assertEqual(d.resume_failure_hint.__module__, "dispatcher_modules.git_sync")
        tmpl = ps.default_text("role/accept")
        ps.save_override("role/accept", "本机覆盖开头\n" + tmpl)
        self.assertTrue(ac.role_prompt("accept").startswith("本机覆盖开头\n"))
        ps.delete_override("role/accept")
        self.assertEqual(ac.role_prompt("accept"), tmpl)

    def test_kill_redline_override_used(self):
        ps.save_override("system/kill-redline",
                         ps.default_text("system/kill-redline") + "\n本机追加一句。")
        self.assertIn("本机追加一句。", ac.append_kill_redline("P", 99))
        self.assertIn("PID=99", ac.append_kill_redline("P", 99))


if __name__ == "__main__":
    unittest.main()
