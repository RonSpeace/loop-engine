"""调度器心跳文件：主循环与飞书长连接状态的读-改-写（看板据此展示调度器存活与飞书通道状态）。"""

import json
import os
import time
from pathlib import Path

from dispatcher_modules.workspace import in_test_env
from dispatcher_modules.runtime import D, log, threading


def load_heartbeat():
    """读心跳 dict；文件缺失/解析失败返回 None（#373：读 last_shutdown 扩展字段入口）"""
    try:
        return json.loads(D.HEARTBEAT_FILE.read_text())
    except Exception:
        return None

_heartbeat_lock = threading.Lock()   # #23：心跳读-改-写串行化（ws 回调线程与主循环并发写）


_PROD_HEARTBEAT_FILE = D.REPO_DIR / "data" / "dispatcher_heartbeat.json"


def _heartbeat_write_locked(hb):
    """写心跳（调用方须已持 _heartbeat_lock）：临时文件 + 原子替换。
    评审建议修复：`os.replace` 失败时清掉 `.tmp`（否则残留半成品文件，异常退出窗口内心跳不可读）。
    测试隔离防线：测试进程（unittest 已加载）写「生产心跳路径」一律拒绝——防止测试里未 patch
    HEARTBEAT_FILE 的 main()/init_feishu 分支把看板飞书状态写成「显式停用」。"""
    if in_test_env() and Path(D.HEARTBEAT_FILE) == _PROD_HEARTBEAT_FILE:
        log.warning("测试进程拒绝写生产心跳文件（测试隔离防线；请 patch dispatcher.HEARTBEAT_FILE）: %s",
                    D.HEARTBEAT_FILE)
        return
    tmp = D.HEARTBEAT_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(hb))
        os.replace(tmp, D.HEARTBEAT_FILE)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        raise


def write_heartbeat(extra=None, update_poll=True, reset_feishu=False):
    """每轮轮询结束写心跳（原子替换，供看板页健康角标判断调度器是否存活/卡住）。
    #373：merge 旧心跳保留 last_shutdown（信号退出记录），extra 可选合并写入。
    #23：读-改-写纳入进程内写锁；update_poll=False 用于「只改字段、不刷新轮询时间」的写入
    （退出记录等）——既有调用缺省行为逐字不变。
    #40：锁内校正 pid（心跳 pid 必须指向当前写下心跳的进程，旧进程残留值覆盖掉）；
    **不动 boot_at**（只有启动首写更新它，exec 自重启靠 boot_at 变化判定已发生）。
    reset_feishu=True（仅 main 启动首写）：把 feishu 整体换成 `{phase: starting, connected: false}`
    ——**禁止新进程在 init_feishu 之前带着上一进程的 connected=true 被看板读走**（随后 init_feishu
    写入真实状态）；last_poll_at 一并刷新：本进程已活着，避免「重启后到首轮轮询前」显示过期心跳。"""
    with _heartbeat_lock:
        hb = D.load_heartbeat() or {}
        if update_poll:
            hb["last_poll_at"] = time.time()
        if extra:
            hb.update(extra)
        if hb.get("pid") != os.getpid():
            hb["pid"] = os.getpid()
        if reset_feishu:
            hb["feishu"] = {"phase": "starting", "connected": False, "enabled": None,
                            "updated_at": time.time()}
        _heartbeat_write_locked(hb)


def _update_feishu_status(**updates):
    """#23 合并写心跳 feishu 子状态（与 write_heartbeat 同锁，防 ws 线程与主循环互相覆盖字段）。
    写失败只告警：绝不因心跳写入问题阻断长连接或主循环。"""
    try:
        with _heartbeat_lock:
            hb = D.load_heartbeat() or {}
            st = hb.get("feishu")
            if not isinstance(st, dict):
                st = {}
            st.update(updates)
            st["updated_at"] = time.time()
            hb["feishu"] = st
            _heartbeat_write_locked(hb)
    except Exception as e:  # noqa: BLE001
        log.warning(f"写飞书心跳状态失败: {e}")

