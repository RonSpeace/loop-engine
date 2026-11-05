#!/usr/bin/env python3
"""英文界面「不漏中文」浏览器级检查：在真实 Chrome 里以英文打开看板，逐个进入主要视图，
找出可见的中文界面文案（文本节点与 title/placeholder/aria-label 属性）。

用户数据区（卡片标题、需求详情、agent 输出等）不算泄漏——口径直接取 static/i18n.js 暴露的
window.LOOP_I18N_ZONES（与翻译层同一份 SKIP / EXACT_ONLY / LABEL_ONLY 列表），不另抄。
在翻译层跳过区之外出现中文 = 泄漏：要么缺译文，要么该节点其实是数据却没标 data-i18n-skip。

用法（需先起种子测试实例）：
    scripts/test_instance.sh start 8931 --seed
    .venv/bin/python scripts/i18n_browser_check.py --url http://127.0.0.1:8931/
退出码：0 = 无泄漏；1 = 有泄漏（逐条打印「视图 · 文案」）；2 = 环境问题（页面打不开 / 无 Chrome）。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from visual_gate import Browser  # noqa: E402

SCAN = r"""(() => {
  const Z = window.LOOP_I18N_ZONES;
  if (!Z) return {error: 'window.LOOP_I18N_ZONES 缺失（i18n.js 未加载？）'};
  const HAN = /[一-鿿]/, out = new Set();
  const data = [Z.skip, Z.exactOnly, Z.labelOnly].join(',');
  const isData = el => !el.closest(Z.force) && !!el.closest(data);
  const visible = el => {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = w.nextNode())) {
    const p = n.parentElement;
    if (!p || !HAN.test(n.nodeValue) || isData(p) || !visible(p)) continue;
    out.add(n.nodeValue.trim().slice(0, 120));
  }
  document.querySelectorAll('[title],[placeholder],[aria-label]').forEach(el => {
    if (isData(el) || !visible(el)) return;
    for (const a of Z.attrs) {
      const v = el.getAttribute(a);
      if (v && HAN.test(v)) out.add('@' + a + ': ' + v.slice(0, 120));
    }
  });
  return {leaks: [...out]};
})()"""

WAIT = "new Promise(r => setTimeout(() => r(1), {ms}))"

# (视图名, 进入该视图的脚本列表)；脚本在页面里按序执行，每步后等待渲染
VIEWS = [
    ("board", []),
    ("detail", ["(() => { const i = items.find(x => x.status === 'doing') || items[0]; if (i) selectReq(i.id); return 1; })()"]),
    ("detail-context", ["(() => { const i = items.find(x => x.status === 'doing') || items[0]; if (i) selectReq(i.id); setDetailTab('context'); return 1; })()"]),
    ("detail-done", ["(() => { setDetailTab('summary'); const i = items.find(x => x.status === 'done'); if (i) selectReq(i.id); return 1; })()"]),
    ("dispatcher-replay", ["(() => { const i = items.find(x => x.dispatcher_count > 0); if (i) selectDispatcher(i.id); return 1; })()"]),
    ("chat-create", ["(() => { openChatFor(autoProjects[0]); return 1; })()"]),
    ("inbox", ["(() => { const b = document.getElementById('interactionInboxButton'); if (b) b.click(); return 1; })()"]),
    ("shortcut-help", ["(() => { document.dispatchEvent(new KeyboardEvent('keydown', {key: '?'})); return 1; })()"]),
] + [("settings-" + t, ["(() => { openSettingsDrawer(); switchSettingsTab('%s'); return 1; })()" % t])
     for t in ("global", "model", "feishu", "prompts", "skills")]


def run(url, width, height, settle_ms):
    report = {}
    with Browser() as br:
        for name, steps in VIEWS:
            ctx = br.send("Target.createBrowserContext")["browserContextId"]
            tid = br.send("Target.createTarget", {"url": "about:blank", "browserContextId": ctx})["targetId"]
            sid = br.send("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]

            def cmd(m, p=None):
                return br.send(m, p, session=sid, timeout=30)

            def ev(expr):
                r = cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
                if r.get("exceptionDetails"):
                    raise RuntimeError(json.dumps(r["exceptionDetails"], ensure_ascii=False)[:300])
                return r.get("result", {}).get("value")

            cmd("Page.enable"); cmd("Runtime.enable")
            cmd("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1,
                                                       "mobile": width <= 720})
            cmd("Page.addScriptToEvaluateOnNewDocument",
                {"source": "try{localStorage.setItem('reqBoardLang','en')}catch(e){}"})
            cmd("Page.navigate", {"url": url})
            # 等首屏数据就绪（项目清单 + 需求列表）
            ev("new Promise((ok, no) => { const t0 = Date.now(); (function poll() {"
               " if (typeof items !== 'undefined' && items.length && autoProjects.length) return ok(1);"
               " if (Date.now() - t0 > 20000) return no(new Error('页面数据 20s 未就绪')); setTimeout(poll, 200); })(); })")
            for s in steps:
                ev(s)
                ev(WAIT.format(ms=settle_ms))
            ev(WAIT.format(ms=settle_ms))
            res = ev(SCAN)
            if res.get("error"):
                raise RuntimeError(res["error"])
            report[name] = res["leaks"]
            br.send("Target.disposeBrowserContext", {"browserContextId": ctx})
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8931/")
    ap.add_argument("--settle-ms", type=int, default=1200)
    ap.add_argument("--json", action="store_true", help="以 JSON 输出全部视图结果")
    a = ap.parse_args(argv)
    total = {}
    try:
        for w, h in ((1440, 900), (390, 844)):
            for view, leaks in run(a.url, w, h, a.settle_ms).items():
                for t in leaks:
                    total.setdefault(t, []).append(f"{view}@{w}")
    except Exception as e:  # 环境问题与泄漏区分开
        print(f"环境问题：{e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(total, ensure_ascii=False, indent=1))
    if not total:
        print("英文界面无中文泄漏（%d 个视图 × 桌面/手机）" % len(VIEWS))
        return 0
    print("英文界面发现 %d 处中文：" % len(total))
    for t, where in sorted(total.items()):
        print(f"  {t}    ← {', '.join(sorted(set(where)))}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
