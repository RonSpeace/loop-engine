"""页面事件流 SSE 推送 / 订阅（#32 拆分 Phase 1）。

可被测试整体改写的模块级状态（开关/上限/订阅表/锁/计数）经 runtime.resolve 晚绑定
读取 board 当前值；发布序号以 board 模块属性为准（自增经 bump 写回）。
"""
from __future__ import annotations

import os
import queue
import select
import socket
import threading
import time

from board_modules.runtime import bump, resolve


# ==================== #416：页面事件流 SSE 推送（进程内发布订阅） ====================
# 设计：poke-only——订阅方收到的只是「某主题有变更」的轻量通知，前端收到后仍走既有 GET
# 增量接口拉取并渲染（不新建数据通道）；纯内存、无 DB 操作、不持 DB 连接。
# 单进程前提成立（app.run(threaded=True)，仅本机 127.0.0.1 使用）；进程重启订阅表清空，
# 前端重连后由首帧 init 对齐通知 + GET 补齐。
# #417 断开即释放：生成器空转期以 resolve("_SSE_PROBE_SEC") 时间片醒来，用 select+MSG_PEEK 做不写流的
# fd 级探测（select 超时 0、PEEK 不消费；werkzeug 在 environ 注入 'werkzeug.socket'，与响应写
# 同线程），对端 FIN/RST 即结束生成器 → finally 释放槽位（≤resolve("_SSE_PROBE_SEC")）；对端失联但无
# FIN/RST 的半开连接仍由「下次写失败」（poke 或 15s 心跳）兜底释放。
_SSE_ENABLED = os.environ.get('REQ_BOARD_SSE', '1') != '0'  # 服务端总开关（REQ_BOARD_SSE=0 关闭）
_SSE_MAX_CONNS = 16      # 连接上限，超限返回 503 交前端降级为轮询
_SSE_HEARTBEAT_SEC = 15  # 心跳注释行间隔（保持代理/浏览器不判空闲断连）
_SSE_PROBE_SEC = 0.25    # 空转探测节拍（断开发现延迟上界；测试可覆写，见 /api/sse-stats）
_SSE_QUEUE_MAX = 64      # 单连接待发帧上限（超出丢最旧，poke 可合并语义）
_sse_lock = threading.Lock()
_sse_subs = set()        # {_SSEConn}
# 发布序号 _sse_seq 单一源在 board 模块（经 runtime.resolve 读、bump 写回），本模块不留副本。
# 累计计数（进程内，重启清零）：opened/rejected 在 _sse_acquire 记，released 在 release 首次生效时记，
# released_by_probe = 其中经 fd 探测发现的断开数（证明真实浏览器下探测路径生效），见 GET /api/sse-stats
_SSE_STATS = {'opened': 0, 'released': 0, 'rejected': 0, 'released_by_probe': 0}


class _SSEConn:
    """一条 SSE 订阅（一个浏览器连接 = 一个实例），队列承载待发 poke。"""

    def __init__(self, req_id=None, sock=None):
        self.req = req_id
        self.sock = sock   # 连接 socket（werkzeug environ 注入；取不到 = None → 探测关闭）
        self.queue = queue.Queue(maxsize=resolve("_SSE_QUEUE_MAX"))
        self.released = False

    def put(self, payload):
        try:
            self.queue.put_nowait(payload)
        except queue.Full:
            try:
                self.queue.get_nowait()   # 丢最旧，保证最新 poke 一定送达
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(payload)
            except queue.Full:
                pass

    def release(self, reason=None):
        """幂等释放槽位（生成器 finally 与迭代器 close 双路径都会调用）。

        reason='probe' = 由 fd 探测发现对端断开（/api/sse-stats 单独计数，验证探测真实生效）。
        """
        with resolve("_sse_lock"):
            if self.released:
                return
            self.released = True
            resolve("_sse_subs").discard(self)
            resolve("_SSE_STATS")['released'] += 1
            if reason == 'probe':
                resolve("_SSE_STATS")['released_by_probe'] += 1


def _sse_publish(topic, req_id=None, chat_id=None):
    """数据库提交后发布变更通知。无订阅者时快速返回（不分配序号、不入队）；只碰内存队列。"""
    with resolve("_sse_lock"):
        if not resolve("_sse_subs"):
            return
        bump("_sse_seq")
        payload = {'topic': topic, 'seq': resolve("_sse_seq"), 'ts': time.time()}
        if req_id is not None:
            payload['req_id'] = req_id
        if chat_id is not None:
            payload['chat_id'] = chat_id
        # 匹配规则：proc/reqchat 按订阅 req 过滤；sessions/chat 页级广播（前端按 chat_id 自筛）
        for sub in list(resolve("_sse_subs")):
            if sub.released:
                continue
            if topic in ('proc', 'reqchat'):
                if req_id is None or sub.req != req_id:
                    continue
            sub.put(payload)


def _sse_acquire(req_id=None, sock=None):
    """注册槽位；超限返回 None（调用方回 503）。sock = 连接 socket（供 fd 断开探测）。"""
    with resolve("_sse_lock"):
        if len(resolve("_sse_subs")) >= resolve("_SSE_MAX_CONNS"):
            resolve("_SSE_STATS")['rejected'] += 1
            return None
        conn = _SSEConn(req_id, sock)
        resolve("_sse_subs").add(conn)
        resolve("_SSE_STATS")['opened'] += 1
        return conn


def _sse_peer_gone(sock):
    """fd 级探测对端是否已断开：可读且 MSG_PEEK 得 EOF（收到 FIN）或 RST → True。不写流、不消费数据。

    sock=None（无 werkzeug.socket：test_client / 非 werkzeug 部署）→ 一律 False（探测关闭，回落
    「下次写失败」的既有语义）；探测异常内部消化，绝不上抛。
    """
    if sock is None:
        return False
    try:
        readable, _, _ = select.select([sock], [], [], 0)   # 不阻塞：只问「现在可读吗」
    except (OSError, ValueError):
        return True    # fd 已失效（已关闭/被回收）
    if not readable:
        return False   # 对端存活：无 FIN/RST 到达
    try:
        data = sock.recv(1, socket.MSG_PEEK)   # 只窥探，不消费（rfile 与响应写均不受影响）
    except (BlockingIOError, InterruptedError):
        return False
    except (OSError, ValueError):
        return True    # RST / fd 已失效
    return data == b''   # EOF（FIN）= 断开；有数据（pipelining 等）视为存活


def _sse_stats_snapshot():
    """订阅状态只读快照（GET /api/sse-stats）：槽位/上限/节拍/发布序号/累计计数 + 按订阅需求分布。"""
    with resolve("_sse_lock"):
        by_req = {}
        for sub in resolve("_sse_subs"):
            key = str(sub.req) if sub.req is not None else 'none'
            by_req[key] = by_req.get(key, 0) + 1
        return {
            'enabled': resolve("_SSE_ENABLED"),
            'subs': len(resolve("_sse_subs")),
            'max': resolve("_SSE_MAX_CONNS"),
            'heartbeat_sec': resolve("_SSE_HEARTBEAT_SEC"),
            'probe_sec': resolve("_SSE_PROBE_SEC"),
            'seq': resolve("_sse_seq"),
            'opened': resolve("_SSE_STATS")['opened'],
            'released': resolve("_SSE_STATS")['released'],
            'rejected': resolve("_SSE_STATS")['rejected'],
            'released_by_probe': resolve("_SSE_STATS")['released_by_probe'],
            'subs_by_req': by_req,
        }


class _SSEStream:
    """迭代器包装：close() 里显式 close 生成器 + 释放槽位，覆盖 werkzeug「生成器未启动
    即断开、finally 不执行」的路径，保证订阅表不泄漏。"""

    def __init__(self, gen, conn):
        self._gen = gen
        self._conn = conn

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._gen)

    def close(self):
        try:
            self._gen.close()
        finally:
            self._conn.release()
