#!/usr/bin/env python3
"""req-416 SSE 事件流后端单测：订阅建立/发布（7 发布点）/匹配过滤/连接上限/退订幂等/心跳/开关/页面注入。

#417 增补 T12–T18：断开即释放（fd 探测路径，socketpair 经 environ_overrides 注入 werkzeug.socket）、
无 socket 降级、16 并发断开后新建 200、探测不误伤活跃连接 + 心跳节拍独立、/api/sse-stats 与计数、
_sse_peer_gone 边界矩阵、释放幂等与计数不重复。

#418：T11 补无订阅者语义断言（不分配发布序号 + 有订阅者对照），T19 锁定死参数收口
（_SSEConn/_sse_acquire/_sse_publish 无 session 形参、帧无 session_id、URL session 参数被忽略）。

运行（worktree 内，用 main 工作区的 .venv）：
    .venv/bin/python -m unittest tests.test_sse_stream -v

注意：读流一律 `client.get(..., buffered=False)` + 后台线程逐帧收集（buffered 默认会把 body 整体消费卡死）；
`_sse_subs` 是模块级状态，setUp 清空、tearDown 释放，防用例间污染。
"""
import json
import queue
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 其它测试模块会临时替换 threading.Thread（test_role_guard / test_dispatcher_events_session 打桩
# dispatcher.threading.Thread 实为改共享模块属性）；模块导入时先捕获真身，setUp 复位、建线程只用真身，
# 避免 discover 顺序下 reader 线程被桩掉（帧永不到达）。
_REAL_THREAD = threading.Thread


class SseStreamTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import board as b
        cls.b = b
        cls.tmp = tempfile.mkdtemp(prefix="req416-board-")
        cls._orig_db = b.DB_PATH
        cls._orig_enabled = b._SSE_ENABLED
        b.DB_PATH = Path(cls.tmp) / "test.db"
        b.init_db()
        b.app.config["TESTING"] = True
        cls.client = b.app.test_client()
        cls.req_id = cls.client.post("/api/requirements", json={
            "title": "SSE-T", "project": "req-board", "status": "later",
            "mode": "semi", "phase": "accept"}).get_json()["id"]
        # #419：卡上流程快照（accept 节点）——/sessions 允许角色按快照当前节点判定
        cls.client.patch(f"/api/requirements/{cls.req_id}", json={
            "phase": "accept", "flow": "standard", "node_index": 2, "nodes_json": json.dumps([
                {"role": "tech", "node_type": "artifact", "artifact": "tech.md"},
                {"role": "dev", "node_type": "code"},
                {"role": "accept", "node_type": "artifact", "artifact": "accept.md"},
                {"node_type": "terminal"}])})
        cls.req2_id = cls.client.post("/api/requirements", json={
            "title": "SSE-T2", "project": "req-board", "status": "later"}).get_json()["id"]

    @classmethod
    def tearDownClass(cls):
        cls.b._SSE_ENABLED = cls._orig_enabled
        cls.b.DB_PATH = cls._orig_db
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if threading.Thread is not _REAL_THREAD:
            threading.Thread = _REAL_THREAD   # 复位被其它测试模块污染的全局
        self.b._SSE_ENABLED = True
        self.b._SSE_MAX_CONNS = 16
        self.b._SSE_HEARTBEAT_SEC = 15
        self.b._SSE_PROBE_SEC = 0.25   # #417：用例覆写后跨用例会污染，逐例复位
        self.b._SSE_STATS = {'opened': 0, 'released': 0, 'rejected': 0, 'released_by_probe': 0}
        self._streams = []
        with self.b._sse_lock:
            self.b._sse_subs.clear()
        with self.b.get_db() as conn:
            conn.execute("DELETE FROM agent_events")
            conn.execute("DELETE FROM agent_sessions")
            conn.execute("DELETE FROM chat_events")
            conn.execute("DELETE FROM req_chat_events")
            conn.execute("DELETE FROM chat_sessions")
            conn.execute("DELETE FROM req_chats")
            conn.commit()

    def tearDown(self):
        # 顺序是关键：读线程必须先于 resp.close() 退出。
        # 段错误根因（既有缺陷，2026-10-06 定位）：SSE 生成器阻塞在 conn.queue.get 上时，
        # 若另一个线程调 resp.close()，werkzeug 会对「正在执行的生成器」调 close →
        # GeneratorExit 被抛进正在跑的栈 → CPython 段错误（exit 139，栈顶
        # werkzeug/wsgi.py:276 close ← test_sse_stream tearDown）。
        # 唤醒手段：置 stop 后往该连接的队列投一个载荷，把阻塞在 queue.get 的生成器推过
        # yield；reader 拿到这一帧即见 stop 自行 break，生成器随之停在 yield 处——此刻没有
        # 任何线程在它内部执行，close() 才是安全的（finally 走 conn.release 释放槽位）。
        for e in self._streams:
            e['stop'].set()
            if e['conn'] is not None:
                e['conn'].put({'topic': '_teardown', 'ts': time.time()})
        for e in self._streams:
            e['thread'].join(timeout=2.0)
        for e in self._streams:
            if e['thread'].is_alive():
                continue   # 读线程没退（例如连接未捕获到、唤醒失败）：宁可泄漏这一条流，
                           # 也绝不并发 close —— 那正是段错误的来源
            try:
                e['resp'].close()
            except Exception:
                pass
        with self.b._sse_lock:
            subs = list(self.b._sse_subs)
        for sub in subs:   # 不可在持锁时 release（非重入锁）
            sub.release()

    # ---- 读流基建 ----
    def open_stream(self, req_id=None, session=None, sock=None):
        """建立 SSE 流并后台收集分帧（返回 (resp, frame_queue)）。

        sock：测试服务端 fd 探测时注入 `werkzeug.socket`（socketpair 一端，另一端模拟浏览器对端）。
        """
        query = []
        if req_id is not None:
            query.append(f"req={req_id}")
        if session:
            query.append(f"session={session}")
        url = "/api/stream" + ("?" + "&".join(query) if query else "")
        kwargs = {'environ_overrides': {'werkzeug.socket': sock}} if sock is not None else {}
        with self.b._sse_lock:
            before = set(self.b._sse_subs)
        resp = self.client.get(url, buffered=False, **kwargs)
        with self.b._sse_lock:
            added = set(self.b._sse_subs) - before
        # 本条流对应的连接对象（503/超上限时 _sse_acquire 返回 None → added 为空）。
        # tearDown 要靠它把阻塞在 queue.get 的生成器唤醒，故在此捕获。
        conn = next(iter(added)) if len(added) == 1 else None
        frames = queue.Queue()
        stop = threading.Event()

        def reader():
            try:
                for chunk in resp.response:
                    if stop.is_set():
                        break   # tearDown 已置位：拿到当前帧就退，别再调 __next__ 把生成器往下推
                    frames.put(chunk)
            except Exception as e:   # pragma: no cover
                frames.put(("__error__", repr(e)))

        threading.Thread = _REAL_THREAD  # 恒用真线程（防桩污染导致帧不到达）
        th = _REAL_THREAD(target=reader, daemon=True)
        th.start()
        self._streams.append({'resp': resp, 'frames': frames, 'stop': stop,
                              'conn': conn, 'thread': th})
        return resp, frames

    def wait_frame(self, frames, pred=lambda p: True, timeout=3.0):
        """限时等一个 data 帧；注释行/retry 行跳过；超时返回 None（不挂死）。"""
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                return None
            try:
                chunk = frames.get(timeout=remain)
            except queue.Empty:
                return None
            if isinstance(chunk, bytes):
                chunk = chunk.decode('utf-8', 'replace')
            if not isinstance(chunk, str):
                self.fail(f"读流异常: {chunk}")
            m = re.search(r'^data: (.*)$', chunk, re.M)
            if not m:
                continue
            payload = json.loads(m.group(1))
            if pred(payload):
                return payload

    def wait_ping(self, frames, timeout=3.0):
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                return False
            try:
                chunk = frames.get(timeout=remain)
            except queue.Empty:
                return False
            if isinstance(chunk, bytes):
                chunk = chunk.decode('utf-8', 'replace')
            if isinstance(chunk, str) and ': ping' in chunk:
                return True

    def subs_count(self):
        with self.b._sse_lock:
            return len(self.b._sse_subs)

    def stats(self):
        """读 GET /api/sse-stats（#417 只读观测端点）。"""
        r = self.client.get('/api/sse-stats')
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def wait_subs(self, n, timeout):
        """限时轮询槽位数到 n，返回耗时秒数；超时返回 None。"""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if self.subs_count() == n:
                return time.monotonic() - t0
            time.sleep(0.02)
        return None

    # ---- T1 连接建立 ----
    def test_t1_connect_and_init_frame(self):
        resp, frames = self.open_stream(self.req_id)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/event-stream', resp.headers['Content-Type'])
        first = frames.get(timeout=3)
        if isinstance(first, bytes):
            first = first.decode()
        self.assertIn('retry: 3000', first)   # 首帧重连间隔
        init = self.wait_frame(frames)
        self.assertEqual(init['topic'], 'init')
        self.assertEqual(self.subs_count(), 1)

    # ---- T2 提交后发布（发布点 1） ----
    def test_t2_publish_after_commit(self):
        _, frames = self.open_stream(self.req_id)
        self.assertIsNotNone(self.wait_frame(frames))   # init 先消费
        r = self.client.post(f"/api/requirements/{self.req_id}/events",
                             json={"events": [{"kind": "message", "content": "hello-sse"}]})
        self.assertEqual(r.status_code, 201)
        poke = self.wait_frame(frames, lambda p: p['topic'] == 'proc')
        self.assertIsNotNone(poke, "未收到 proc poke")
        self.assertEqual(poke['req_id'], self.req_id)
        # 发布晚于提交：收到 poke 时 GET 已能读到新事件
        rows = self.client.get(f"/api/requirements/{self.req_id}/events").get_json()
        self.assertIn('hello-sse', [x['content'] for x in rows])

    # ---- T3 七发布点覆盖 ----
    def test_t3_all_publish_points(self):
        _, frames = self.open_stream(self.req_id)
        self.wait_frame(frames)   # init
        # 2) _chat_add_event → chat
        with self.b.get_db() as conn:
            cur = conn.execute("INSERT INTO chat_sessions (project, status, created_at, updated_at)"
                               " VALUES ('req-board', 'running', ?, ?)", (self.b.now_str(), self.b.now_str()))
            chat_id = cur.lastrowid
            conn.commit()
        self.b._chat_add_event(chat_id, 'message', 'c1')
        self.assertEqual(self.wait_frame(frames, lambda p: p['topic'] == 'chat')['chat_id'], chat_id)
        # 5) _chat_set → chat（纯状态变更）
        self.b._chat_set(chat_id, status='idle')
        self.assertEqual(self.wait_frame(frames, lambda p: p['topic'] == 'chat')['chat_id'], chat_id)
        # 3) _reqchat_add_event → reqchat（payload 带 req_id）
        with self.b.get_db() as conn:
            cur = conn.execute("INSERT INTO req_chats (req_id, status, created_at, updated_at)"
                               " VALUES (?, 'running', ?, ?)", (self.req_id, self.b.now_str(), self.b.now_str()))
            rc_id = cur.lastrowid
            conn.commit()
        self.b._reqchat_add_event(rc_id, 'message', 'r1')
        self.assertEqual(self.wait_frame(frames, lambda p: p['topic'] == 'reqchat')['req_id'], self.req_id)
        # 6) _reqchat_set → reqchat
        self.b._reqchat_set(rc_id, status='idle')
        self.assertEqual(self.wait_frame(frames, lambda p: p['topic'] == 'reqchat')['req_id'], self.req_id)
        # 4) /api/agent-sessions 三条路径 → sessions
        self.assertEqual(self.client.post("/api/agent-sessions", json={
            "req_id": self.req_id, "role": "accept", "session_id": "sid-1"}).status_code, 201)
        self.assertIsNotNone(self.wait_frame(frames, lambda p: p['topic'] == 'sessions'))
        row_id = self.client.get("/api/agent-sessions").get_json()[0]['id']
        self.client.post("/api/agent-sessions", json={"id": row_id, "status": "running"})
        self.assertIsNotNone(self.wait_frame(frames, lambda p: p['topic'] == 'sessions'))
        self.client.post("/api/agent-sessions", json={
            "req_id": self.req_id, "role": "accept", "session_id": "sid-1", "status": "idle"})
        self.assertIsNotNone(self.wait_frame(frames, lambda p: p['topic'] == 'sessions'))
        # 7) 首条消息落行（#431：旧 POST /sessions 已下线，发布点随之迁移）
        self.assertEqual(self.client.post(f"/api/requirements/{self.req_id}/sessions/messages",
                                          json={"text": "首条"}).status_code, 201)
        self.assertIsNotNone(self.wait_frame(frames, lambda p: p['topic'] == 'sessions'))
        self.assertEqual(self.client.post(f"/api/requirements/{self.req_id}/sessions",
                                          json={"role": "accept"}).status_code, 410)

    # ---- T4 匹配过滤 ----
    def test_t4_subscription_filter(self):
        _, frames = self.open_stream(self.req_id)
        self.wait_frame(frames)   # init
        self.client.post(f"/api/requirements/{self.req2_id}/events",
                         json={"events": [{"kind": "message", "content": "other-req"}]})
        self.client.post(f"/api/requirements/{self.req_id}/events",
                         json={"events": [{"kind": "message", "content": "mine"}]})
        # 阳性锚点：收到本需求 poke 前的所有 proc 帧都不属于 req2
        seen = []
        poke = self.wait_frame(frames, lambda p: p['topic'] == 'proc' and p['req_id'] == self.req_id)
        self.assertIsNotNone(poke)
        while not frames.empty():
            try:
                chunk = frames.get_nowait()
            except queue.Empty:
                break
            m = re.search(r'^data: (.*)$', chunk, re.M)
            if m:
                seen.append(json.loads(m.group(1)))
        self.assertTrue(all(p.get('req_id') != self.req2_id for p in seen))

    # ---- T5 连接上限 ----
    def test_t5_conn_limit(self):
        self.b._SSE_MAX_CONNS = 2
        self.open_stream(self.req_id)
        self.open_stream(self.req_id)
        self.assertEqual(self.subs_count(), 2)
        resp = self.client.get('/api/stream', buffered=False)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(self.subs_count(), 2)
        # 释放 1 条后可再建
        with self.b._sse_lock:
            victim = list(self.b._sse_subs)[0]
        victim.release()
        self.assertEqual(self.client.get('/api/stream', buffered=False).status_code, 200)

    # ---- T6 退订与幂等释放 ----
    def test_t6_release_idempotent(self):
        # 迭代器 close() 路径（werkzeug 关流时调用）：生成器被关闭 + 槽位释放 + 幂等
        closed = []

        def gen():
            try:
                yield 'data: {}\n\n'
            finally:
                closed.append('gen-closed')

        conn = self.b._SSEConn(self.req_id, None)
        with self.b._sse_lock:
            self.b._sse_subs.add(conn)
        stream = self.b._SSEStream(gen(), conn)
        self.assertTrue(next(stream).startswith('data:'))
        stream.close()
        self.assertEqual(closed, ['gen-closed'])
        self.assertNotIn(conn, self.b._sse_subs)
        stream.close()   # 幂等：重复 close 不报错、不误删他人
        # 已释放连接不再收 poke
        other = self.b._SSEConn(self.req_id, None)
        with self.b._sse_lock:
            self.b._sse_subs.add(other)
        conn.release()
        self.assertIn(other, self.b._sse_subs)
        self.b._sse_publish('proc', req_id=self.req_id)
        self.assertTrue(conn.queue.empty(), "已释放连接仍收到 poke")
        self.assertFalse(other.queue.empty(), "活跃连接未收到 poke")
        # HTTP 层：关闭流（服务端释放）后订阅表清零
        other.release()
        resp, frames = self.open_stream(self.req_id)
        self.wait_frame(frames)
        self.assertEqual(self.subs_count(), 1)
        with self.b._sse_lock:
            live = list(self.b._sse_subs)[0]
        live.release()   # werkzeug 关流路径的等价触发
        self.assertEqual(self.subs_count(), 0, "关闭流后订阅表未清零")

    # ---- T7 服务端开关 ----
    def test_t7_server_switch(self):
        self.b._SSE_ENABLED = False
        self.assertEqual(self.client.get('/api/stream', buffered=False).status_code, 503)
        page = self.client.get('/').get_data(as_text=True)
        # #33：常量改由页面配置脚本注入（static/board.js 读 window.__REQ_BOARD_CFG__.sseEnabled）
        self.assertIn('sseEnabled:false', page)

    # ---- T8 心跳 ----
    def test_t8_heartbeat(self):
        self.b._SSE_HEARTBEAT_SEC = 0.2
        _, frames = self.open_stream(self.req_id)
        self.wait_frame(frames)   # init
        self.assertTrue(self.wait_ping(frames, timeout=3), "未收到 : ping 心跳注释行")

    # ---- T9 页面注入 ----
    def test_t9_page_injection(self):
        self.b._SSE_ENABLED = True
        self.assertIn('sseEnabled:true', self.client.get('/').get_data(as_text=True))

    # ---- T10 首条消息落行发布（#431，第 7 发布点迁移） ----
    def test_t10_empty_session_poke(self):
        _, frames = self.open_stream(self.req_id)
        self.wait_frame(frames)
        self.client.post(f"/api/requirements/{self.req_id}/sessions/messages", json={"text": "首条"})
        poke = self.wait_frame(frames, lambda p: p['topic'] == 'sessions')
        self.assertIsNotNone(poke, "首条消息落行未收到 sessions poke")

    # ---- T11 无订阅者时发布快速返回（不分配序号、不入队、不抛异常） ----
    def test_t11_publish_without_subscribers(self):
        with mock.patch.object(self.b, '_sse_publish') as m:
            self.client.post(f"/api/requirements/{self.req_id}/events",
                             json={"events": [{"kind": "message", "content": "x"}]})
            m.assert_called_once()
        # 无订阅者语义：快速返回——不递增发布序号、不抛异常（旧用例只断言「不抛」）
        self.assertEqual(self.subs_count(), 0)
        seq0 = self.b._sse_seq
        self.b._sse_publish('proc', req_id=self.req_id)
        self.b._sse_publish('sessions')
        self.b._sse_publish('chat', chat_id=9)
        self.assertEqual(self.b._sse_seq, seq0, "无订阅者时仍分配了发布序号")
        # 有订阅者：序号递增、poke 帧真实到达（对照无订阅者分支）
        _, frames = self.open_stream(self.req_id)
        self.wait_frame(frames)
        self.b._sse_publish('proc', req_id=self.req_id)
        poke = self.wait_frame(frames, lambda p: p['topic'] == 'proc')
        self.assertIsNotNone(poke, "有订阅者时未收到 poke")
        self.assertEqual(poke['seq'], seq0 + 1)


    # ---- T12 断开即释放（fd 探测路径，不写流） ----
    def test_t12_disconnect_release_within_1s(self):
        sock_a, sock_b = socket.socketpair()
        try:
            _, frames = self.open_stream(self.req_id, sock=sock_a)
            self.wait_frame(frames)   # init
            self.assertEqual(self.subs_count(), 1)
            s0 = self.stats()
            sock_b.close()            # 模拟浏览器断开（FIN 到达）
            elapsed = self.wait_subs(0, 1.0)
            self.assertIsNotNone(elapsed, "断开后 1s 内未释放槽位")
            time.sleep(0.1)           # 给潜在写路径留落帧窗口
            written = []
            while not frames.empty():
                chunk = frames.get_nowait()
                if isinstance(chunk, bytes):
                    chunk = chunk.decode('utf-8', 'replace')
                if isinstance(chunk, str) and 'data:' in chunk:
                    written.append(chunk)
            self.assertEqual(written, [], "探测释放路径不应再向已断开连接写帧")
            s1 = self.stats()
            self.assertEqual(s1['released'] - s0['released'], 1)
            self.assertEqual(s1['released_by_probe'] - s0['released_by_probe'], 1,
                             "释放未走 fd 探测路径（released_by_probe 未增）")
            self.assertEqual(s1['subs'], 0)
        finally:
            sock_a.close()

    # ---- T13 无 socket 降级路径（语义=改动前） ----
    def test_t13_no_socket_fallback(self):
        self.assertFalse(self.b._sse_peer_gone(None))
        _, frames = self.open_stream(self.req_id)   # 不注入 werkzeug.socket → 探测关闭
        self.wait_frame(frames)
        self.assertEqual(self.subs_count(), 1)
        s = self.stats()
        self.assertEqual(s['released_by_probe'], 0)
        time.sleep(0.6)   # ≥2 个探测节拍：探测关闭不应误判断开
        self.assertEqual(self.subs_count(), 1, "无 socket 时探测误释放了连接")
        with self.b._sse_lock:
            live = list(self.b._sse_subs)[0]
        live.release()
        self.assertEqual(self.subs_count(), 0)
        live.release()    # 幂等
        self.assertEqual(self.subs_count(), 0)

    # ---- T14 16 条并发断开 ≤2s + 释放后可新建 200 ----
    def test_t14_16_concurrent_disconnect_release(self):
        self.b._SSE_MAX_CONNS = 16
        pairs = []
        try:
            for _ in range(16):
                a, b = socket.socketpair()
                pairs.append((a, b))
                self.open_stream(self.req_id, sock=a)
            self.assertEqual(self.subs_count(), 16)
            self.assertEqual(self.client.get('/api/stream', buffered=False).status_code, 503)
            for _, b in pairs:
                b.close()                 # 全部对端断开
            elapsed = self.wait_subs(0, 2.0)
            self.assertIsNotNone(elapsed, "16 条并发断开 2s 内未全部释放槽位")
            s = self.stats()
            self.assertEqual(s['subs'], 0)
            self.assertEqual(s['released_by_probe'] - 0, 16, "16 条未全部走探测路径释放")
            a2, b2 = socket.socketpair()
            pairs.append((a2, b2))
            resp, frames = self.open_stream(self.req_id, sock=a2)
            self.assertEqual(resp.status_code, 200, "槽位释放后新建连接仍被拒")
            self.assertIsNotNone(self.wait_frame(frames))
        finally:
            for a, b in pairs:
                a.close()
                try:
                    b.close()
                except OSError:
                    pass

    # ---- T15 探测不误伤活跃连接 + 心跳节拍独立 ----
    def test_t15_probe_keeps_live_conn_and_ping_pace(self):
        self.b._SSE_HEARTBEAT_SEC = 0.3
        a, b = socket.socketpair()
        try:
            _, frames = self.open_stream(self.req_id, sock=a)
            self.wait_frame(frames)   # init
            time.sleep(0.9)           # ≥3 个探测节拍
            self.assertEqual(self.subs_count(), 1, "活跃连接被探测误释放")
            self.assertTrue(self.wait_ping(frames, timeout=2), "探测开启后未按心跳节拍收到 : ping")
            b.send(b'X')              # 对端有数据：应视为存活且不被消费
            time.sleep(0.6)           # ≥2 个探测节拍
            self.assertEqual(self.subs_count(), 1, "对端有数据时被误判断开")
            a.settimeout(1.0)
            self.assertEqual(a.recv(1), b'X', "探测消费了对端数据（MSG_PEEK 应不消费）")
        finally:
            a.close()
            b.close()

    # ---- T16 /api/sse-stats 端点与计数 ----
    def test_t16_sse_stats_endpoint(self):
        keys = ('enabled', 'subs', 'max', 'heartbeat_sec', 'probe_sec', 'seq',
                'opened', 'released', 'rejected', 'released_by_probe', 'subs_by_req')
        s0 = self.stats()
        for k in keys:
            self.assertIn(k, s0)
        self.assertTrue(s0['enabled'])
        self.assertEqual((s0['subs'], s0['subs_by_req']), (0, {}))
        self.assertEqual(s0['max'], 16)
        self.assertEqual(s0['heartbeat_sec'], 15)
        self.assertAlmostEqual(s0['probe_sec'], self.b._SSE_PROBE_SEC)
        pairs = []
        try:
            for rid in (self.req_id, self.req2_id):
                a, b = socket.socketpair()
                pairs.append((a, b))
                self.open_stream(rid, sock=a)
            s1 = self.stats()
            self.assertEqual(s1['subs'], 2)
            self.assertEqual(s1['opened'] - s0['opened'], 2)
            self.assertEqual(s1['subs_by_req'], {str(self.req_id): 1, str(self.req2_id): 1})
            self.b._SSE_MAX_CONNS = 2
            self.assertEqual(self.client.get('/api/stream', buffered=False).status_code, 503)
            s2 = self.stats()
            self.assertEqual(s2['rejected'] - s1['rejected'], 1)
            self.assertEqual(s2['max'], 2)
            pairs[0][1].close()       # 断开一条 → 探测释放
            self.assertIsNotNone(self.wait_subs(1, 1.0))
            s3 = self.stats()
            self.assertEqual(s3['released'] - s0['released'], 1)
            self.assertEqual(s3['released_by_probe'] - s0['released_by_probe'], 1)
            self.assertEqual(s3['subs_by_req'], {str(self.req2_id): 1})
            self.b._SSE_ENABLED = False
            s4 = self.stats()
            self.assertFalse(s4['enabled'])
            self.assertEqual(s4['subs'], 1, "关闭开关不应影响既有连接")
        finally:
            for a, b in pairs:
                a.close()
                try:
                    b.close()
                except OSError:
                    pass

    # ---- T17 _sse_peer_gone 边界矩阵（单测） ----
    def test_t17_peer_gone_matrix(self):
        b = self.b
        self.assertFalse(b._sse_peer_gone(None))
        a, peer = socket.socketpair()
        try:
            self.assertFalse(b._sse_peer_gone(a), "无数据/无 FIN 被误判断开")
            peer.send(b'Z')
            self.assertFalse(b._sse_peer_gone(a), "有数据被误判断开")
            a.settimeout(1.0)
            self.assertEqual(a.recv(1), b'Z', "探测消费了对端数据")
            peer.close()
            deadline = time.time() + 1.0
            gone = False
            while time.time() < deadline:
                if b._sse_peer_gone(a):
                    gone = True
                    break
                time.sleep(0.02)
            self.assertTrue(gone, "对端 FIN 后未判为断开")
        finally:
            a.close()

        class _FakeSock:
            def __init__(self, exc):
                self.exc = exc

            def recv(self, n, flags=0):
                raise self.exc

        d, e = socket.socketpair()
        d.close()
        self.assertTrue(b._sse_peer_gone(d), "已关闭 fd 未判为断开")   # select 抛 ValueError
        e.close()
        with mock.patch.object(b.select, 'select', lambda r, w, x, t: (r, [], [])):
            self.assertTrue(b._sse_peer_gone(_FakeSock(ConnectionResetError())), "RST 未判为断开")
            self.assertTrue(b._sse_peer_gone(_FakeSock(ValueError())), "fd 失效未判为断开")
            self.assertFalse(b._sse_peer_gone(_FakeSock(BlockingIOError())), "BlockingIOError 被误判断开")
            self.assertFalse(b._sse_peer_gone(_FakeSock(InterruptedError())), "InterruptedError 被误判断开")
        with mock.patch.object(b.select, 'select', mock.Mock(side_effect=OSError('bad fd'))):
            self.assertTrue(b._sse_peer_gone(_FakeSock(None)), "select 异常未判为断开")

    # ---- T18 幂等与计数不重复（双路径触发） ----
    def test_t18_release_idempotent_counters(self):
        a1, b1 = socket.socketpair()
        a2, b2 = socket.socketpair()
        try:
            _, f1 = self.open_stream(self.req_id, sock=a1)
            _, f2 = self.open_stream(self.req_id, sock=a2)
            self.wait_frame(f1)
            self.wait_frame(f2)
            with self.b._sse_lock:
                before = set(self.b._sse_subs)
            s0 = self.stats()
            b1.close()
            self.assertIsNotNone(self.wait_subs(1, 1.0), "断开后未释放槽位")
            with self.b._sse_lock:
                after = set(self.b._sse_subs)
            gone = (before - after).pop()
            s1 = self.stats()
            self.assertEqual(s1['released'] - s0['released'], 1)
            self.assertEqual(s1['released_by_probe'] - s0['released_by_probe'], 1)
            gone.release()           # 双路径兜底：再 release 不重复计数
            gone.release('probe')
            s2 = self.stats()
            self.assertEqual(s2['released'], s1['released'], "重复释放被二次计数")
            self.assertEqual(s2['released_by_probe'], s1['released_by_probe'])
            self.assertEqual(self.subs_count(), 1, "误删他人槽位")
            live = after.pop()
            self.assertIn(live, before)
            self.b._sse_publish('proc', req_id=self.req_id)
            self.assertFalse(live.queue.empty(), "活跃连接未收到 poke")
            self.assertTrue(gone.queue.empty(), "已释放连接仍收到 poke")
        finally:
            a1.close()
            a2.close()
            b1.close()
            b2.close()


    # ---- T19 死参数收口（#418）：订阅/发布无 session 形参，URL 参数保留但被忽略 ----
    def test_t19_no_session_plumbing(self):
        import inspect
        self.assertNotIn('session_id', inspect.signature(self.b._SSEConn.__init__).parameters)
        self.assertNotIn('session_id', inspect.signature(self.b._sse_acquire).parameters)
        self.assertNotIn('session_id', inspect.signature(self.b._sse_publish).parameters)
        self.assertFalse(hasattr(self.b._SSEConn(self.req_id), 'session'), "订阅对象仍带 session 属性")
        # URL 兼容：session 查询参数被忽略（不报错、不改变订阅与分发；前端 sseStreamUrl 仍携带）
        resp, frames = self.open_stream(self.req_id, session='sid-ignored')
        self.assertEqual(resp.status_code, 200)
        self.wait_frame(frames)
        self.b._sse_publish('proc', req_id=self.req_id)
        poke = self.wait_frame(frames, lambda p: p['topic'] == 'proc')
        self.assertIsNotNone(poke, "带 session 参数的连接未收到 poke")
        self.assertNotIn('session_id', poke, "帧里仍带死字段 session_id")


if __name__ == '__main__':
    unittest.main()
