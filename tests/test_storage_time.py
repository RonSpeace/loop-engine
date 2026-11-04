"""存储时区可移植性：跨进程配置、夏令时与读写一致性回归。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import timezone
from unittest.mock import patch

import storage_time as st


class StorageTimeTest(unittest.TestCase):
    def test_config_validation(self):
        self.assertIsNone(st.configured_timezone(''))
        for value in ('UTC', 'Asia/Shanghai', 'America/New_York', '+08:00', '-05:30'):
            self.assertIsNotNone(st.configured_timezone(value))
        for value in ('bogus', '+24:00', '+00:60', '8', '+8:00'):
            with self.assertRaisesRegex(ValueError, 'REQ_BOARD_TZ'):
                st.configured_timezone(value)

    def test_explicit_timezone_historical_dst_and_offsets(self):
        cases = [('America/New_York', '2026-01-15 12:00:00', 17),
                 ('America/New_York', '2026-07-15 12:00:00', 16),
                 ('+08:00', '2026-01-15 12:00:00', 4),
                 ('-05:30', '2026-01-15 12:00:00', 17.5)]
        for zone, text, utc_hour in cases:
            with self.subTest(zone=zone, text=text), patch.object(st, 'STORAGE_TZ', st.configured_timezone(zone)):
                value = st.parse_storage_time(text)
                utc = value.astimezone(timezone.utc)
                self.assertEqual(utc.hour + utc.minute / 60, utc_hour)
                self.assertEqual(st.format_storage_time(value.timestamp()), text)

    def test_local_and_explicit_cross_process_round_trip(self):
        code = '''
import json, time
from storage_time import parse_storage_time, format_storage_time
import board
stamp = board.now_str()
print(json.dumps([abs(board._ts_ms(stamp) / 1000 - time.time()),
                  format_storage_time(0),
                  parse_storage_time('2026-07-15 12:00:00').utcoffset().total_seconds()]))
'''
        for machine, configured, epoch_text, summer_offset in (
            ('UTC', '', '1970-01-01 00:00:00', 0),
            ('Asia/Shanghai', '', '1970-01-01 08:00:00', 28800),
            ('UTC', 'Asia/Shanghai', '1970-01-01 08:00:00', 28800),
            ('Asia/Shanghai', '-05:30', '1969-12-31 18:30:00', -19800),
            ('America/New_York', '', '1969-12-31 19:00:00', -14400),
        ):
            with self.subTest(machine=machine, configured=configured), tempfile.TemporaryDirectory() as tmp:
                env = dict(os.environ, TZ=machine, REQ_BOARD_TZ=configured,
                           REQ_BOARD_DATA=tmp, REQ_BOARD_FREEZE_TIME='')
                run = subprocess.run([sys.executable, '-c', code], env=env,
                                     cwd=Path(__file__).resolve().parent.parent,
                                     capture_output=True, text=True, check=True)
                age, epoch, offset = json.loads(run.stdout.strip().splitlines()[-1])
                self.assertLess(age, 3)
                self.assertEqual(epoch, epoch_text)
                self.assertEqual(offset, summer_offset)

    def test_event_apis_supply_epoch_and_chat_creation_uses_storage_time(self):
        import board
        from board_modules.services.reqchat_runtime import _reqchat_create
        epoch = 1767323045
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(board, 'DB_PATH', Path(tmp) / 'board.db'), \
                patch.object(board, 'FREEZE_TIME_STR', None), \
                patch.object(st, 'STORAGE_TZ', st.configured_timezone('-05:30')):
            board.init_db()
            now = board.now_str()
            req = board.app.test_client().post('/api/requirements', json={
                'title': '时区隔离测试', 'project': 'tz-test', 'status': 'later'}).get_json()
            rid = req['id']
            chat_id = _reqchat_create(rid)
            stamp = st.format_storage_time(epoch)
            with board.get_db() as conn:
                row = conn.execute('SELECT created_at FROM req_chats WHERE id = ?', (chat_id,)).fetchone()
                self.assertLess(abs(board._ts_ms(row['created_at']) - board._ts_ms(now)), 3000)
                conn.execute("INSERT INTO req_chat_events (req_chat_id, kind, content, ts) VALUES (?, 'user', 'x', ?)", (chat_id, stamp))
                conn.execute("INSERT INTO chat_sessions (project, status) VALUES ('tz-test', 'idle')")
                regular = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                conn.execute("INSERT INTO chat_events (chat_id, kind, content, ts) VALUES (?, 'user', 'x', ?)", (regular, stamp))
                conn.execute("INSERT INTO dispatcher_events (req_id, kind, content, ts) VALUES (?, 'phase', 'x', ?)", (rid, stamp))
                conn.commit()
            client = board.app.test_client()
            for path in (f'/api/chat/{regular}/events', f'/api/requirements/{rid}/dispatcher-events'):
                self.assertEqual(client.get(path).get_json()[-1]['ts_ms'], epoch * 1000)
            reply = client.get(f'/api/requirements/{rid}/chat/events').get_json()
            self.assertEqual(reply['events'][-1]['ts_ms'], epoch * 1000)
