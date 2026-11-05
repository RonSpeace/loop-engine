"""交互收件箱复用既有状态；终态与已提交裁决不重复催办。"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def inbox(reqs, confirms=None, merges=None, health=None):
    if not shutil.which('node'):
        pytest.skip('需要 node')
    source = (ROOT / 'static/board/57-interaction.js').read_text()
    function = source.split('        function interactionInboxRows(', 1)[1].split('        function interactionRenderInbox', 1)[0]
    script = 'function interactionInboxRows(' + function
    script += '\nconsole.log(JSON.stringify(interactionInboxRows(...JSON.parse(process.argv[1]))));'
    return json.loads(subprocess.check_output(['node', '-e', script, json.dumps([reqs, confirms or {}, merges or {}, health])], text=True))


def test_pending_decisions_and_merge_identity():
    reqs = [{'id': n, 'title': str(n), 'status': 'later'} for n in range(1, 5)]
    result = inbox(reqs, {'1': {'question': '收尾校验未通过，申报豁免'}, '2': {'over_limit': True}}, {'3': {}})
    assert [r['id'] for r in result] == [1, 2, 3]
    assert result[0]['reason'] == '收尾校验需要你裁决'
    assert result[1]['reason'] == '评审多次打回，需要人工决定'
    assert 'Git 身份' in result[2]['reason']


def test_terminal_consumed_and_disabled_are_not_actionable():
    reqs = [{'id': 1, 'title': 'done', 'status': 'done'}, {'id': 2, 'title': 'pending', 'status': 'later'}]
    assert inbox(reqs, {'1': {}, '2': {'action': 'pass'}}, health={'ok': False, 'state': 'disabled'}) == []


def test_external_and_failed_final_checks_and_health():
    reqs = [
        {'id': 1, 'title': 'external', 'status': 'later', 'summary': '技术设计由外部主 Agent 负责，等待其产出后继续。'},
        {'id': 2, 'title': 'gate', 'status': 'later', 'summary': '产物校验失败'},
        {'id': 3, 'title': 'old external', 'status': 'open', 'summary': '技术设计由外部主 Agent 负责'},
    ]
    result = inbox(reqs, health={'ok': False, 'state': 'stale'})
    assert [r['id'] for r in result] == [1, 2, None]


def test_interaction_dictionary_covers_labels_and_reasons():
    import re
    source = (ROOT / 'static/board/57-interaction.js').read_text()
    dictionary = (ROOT / 'static/i18n.en.js').read_text().split('window.LOOP_INTERACTION_EN = ', 1)[1]
    dictionary = json.loads(dictionary.strip().rstrip(';'))
    labels = set(re.findall(r"interactionText\('([^']+)'\)", source))
    labels.update(re.findall(r"reason = '([^']+)'", source))
    assert labels <= dictionary.keys()
    assert all(not re.search('[一-鿿]', value) for value in dictionary.values())
