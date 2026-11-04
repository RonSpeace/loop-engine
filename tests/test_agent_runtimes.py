"""运行体选择/能力闸门/异构审批输入；全部在临时文件内，不启动真实 agent。"""
import json
import os
from pathlib import Path
from unittest import mock

import pytest
import acp_shared as ac


@pytest.fixture
def runtime_config(tmp_path, monkeypatch):
    path = tmp_path / 'projects.json'
    path.write_text('{}')
    monkeypatch.setattr(ac, 'PROJECTS_FILE', path)
    monkeypatch.setattr(ac, 'FLOW_CONFIG', {'roles': {'dev': {}}})
    monkeypatch.delenv('LOOP_AGENT_RUNTIME', raising=False)
    return path


def test_selection_chain(runtime_config, monkeypatch):
    assert ac.role_runtime_detail('dev', 'p') == ('kimi', 'global')
    monkeypatch.setenv('LOOP_AGENT_RUNTIME', 'codex')
    assert ac.role_runtime_detail('dev', 'p') == ('codex', 'env')
    ac.FLOW_CONFIG['roles']['dev']['runtime'] = 'claude-code'
    assert ac.role_runtime_detail('dev', 'p') == ('claude-code', 'role')
    runtime_config.write_text(json.dumps({'p': {'runtime': 'codex'}}))
    assert ac.role_runtime_detail('dev', 'p') == ('codex', 'project')
    runtime_config.write_text(json.dumps({'p': {'runtime': 'codex', 'runtimes': {'dev': 'kimi'}}}))
    assert ac.role_runtime_detail('dev', 'p') == ('kimi', 'project_role')


@pytest.mark.parametrize('value', ['typo', '', None, 1, []])
def test_unknown_fails_before_spawn(runtime_config, value):
    runtime_config.write_text(json.dumps({'p': {'runtime': value}}))
    with mock.patch.object(ac.subprocess, 'Popen') as spawn:
        with pytest.raises(ValueError, match='未知 agent runtime'):
            ac.ACPSession(1, '/tmp', None, 'dev', '/tmp', project='p')
        spawn.assert_not_called()


def test_invalid_config_fail_closed(runtime_config):
    for content in ('{', '{"p":{"runtimes": []}}'):
        runtime_config.write_text(content)
        with pytest.raises(ValueError):
            ac.role_runtime('dev', 'p')


@pytest.mark.parametrize('runtime', ['claude-code', 'codex'])
@pytest.mark.parametrize('role', list(ac.ROLE_CAPABILITIES.keys() - {'dev'}))
def test_restricted_roles_refuse_before_spawn(runtime, role):
    with mock.patch.object(ac.subprocess, 'Popen') as spawn:
        with pytest.raises(ValueError, match='拒绝启动受限/只读角色'):
            ac.ACPSession(1, '/tmp', None, role, '/tmp', runtime=runtime)
        spawn.assert_not_called()


@pytest.mark.parametrize('runtime', ['claude-code', 'codex'])
def test_capability_gates(runtime, runtime_config, monkeypatch):
    monkeypatch.setenv('LOOP_AGENT_RUNTIME', runtime)
    with mock.patch.object(ac, 'model_alias_pool', side_effect=AssertionError('不可读取 kimi 池')):
        result = ac.resolve_role_model('dev', 'p')
        assert not result['degraded']
    session = ac.ACPSession.__new__(ac.ACPSession)
    session.runtime_id = runtime
    session.request = mock.Mock(side_effect=AssertionError('不可发送 kimi 方法'))
    session._flush_message = mock.Mock()
    session._apply_model()
    session._apply_permission_mode()
    assert session.collect_usage() is None
    session._flush_message.assert_called_once_with()
    session.request.assert_not_called()
    from board_modules.services.session_runtime import resolve_session_dir
    assert resolve_session_dir('session_abc', runtime=runtime) is None


def test_launch_argv(monkeypatch):
    assert ac.runtime_argv('kimi', '/wrapper') == ['/wrapper', 'acp']
    with mock.patch.object(ac.shutil, 'which', return_value=None):
        assert ac.runtime_argv('claude-code') == ['npx', '-y', '@zed-industries/claude-code-acp@0.16.2']
        assert ac.runtime_argv('codex') == ['npx', '-y', '@zed-industries/codex-acp@0.16.0']
    with mock.patch.object(ac.shutil, 'which', return_value='/bin/codex-acp'):
        assert ac.runtime_argv('codex') == ['/bin/codex-acp']
    monkeypatch.setenv('LOOP_AGENT_CMD_CLAUDE_CODE', '"/tmp/my adapter" --flag')
    assert ac.runtime_argv('claude-code') == ['/tmp/my adapter', '--flag']
    monkeypatch.setenv('LOOP_AGENT_CMD_CODEX', '')
    with pytest.raises(ValueError):
        ac.runtime_argv('codex')


@pytest.mark.parametrize(('runtime', 'tool', 'command'), [
    ('claude-code', {'title': '`ls`', 'rawInput': {'command': 'ls'}}, 'ls'),
    ('codex', {'title': 'List files', 'rawInput': {'command': ['bash', '-lc', 'ls -a']}}, 'ls -a'),
    ('codex', {'title': 'Read file', 'rawInput': {'command': ['cat', 'a b']}}, "cat 'a b'"),
])
def test_command_normalization(runtime, tool, command):
    normalized = ac.normalize_permission_tool(runtime, tool)
    assert normalized['title'] == 'Bash'
    assert normalized['rawInput']['command'] == command
    assert ac.ACPSession._toolcall_text(normalized) == command


@pytest.mark.parametrize('runtime', ['claude-code', 'codex'])
def test_permission_no_unknown_allow_always_or_terminal_bypass(runtime, tmp_path):
    session = ac.ACPSession.__new__(ac.ACPSession)
    session.runtime_id = runtime
    session._terminal_approve = mock.Mock()
    session.workdir = tmp_path
    session.artifact_root = tmp_path
    session.write_roots = [tmp_path]
    options = [{'kind': 'allow_always', 'optionId': 'always'},
               {'kind': 'allow_once', 'optionId': 'once'}]
    tc = {'title': 'safe-looking', 'rawInput': {'command': 'kill 123'}}
    params = {'toolCall': tc, 'options': options}
    assert session._auto_approve(params)['outcome']['optionId'] == 'once'
    session._terminal_approve.assert_called_once_with('kill 123')
    session._terminal_approve.side_effect = PermissionError('redline')
    assert session._auto_approve(params)['outcome']['outcome'] == 'cancelled'
    for tool in ({'title': 'mcp__reqboard-tools__read_file'},
                 {'title': 'Bash', 'rawInput': {'description': 'ls'}},
                 {'title': 'Write', 'writePaths': [str(tmp_path/'safe')],
                  'rawInput': {'file_path': '/etc/passwd', 'content': 'x'}}):
        assert session._auto_approve({'toolCall': tool, 'options': options})['outcome']['outcome'] == 'cancelled'
    params['options'] = options[:1]
    session._terminal_approve.side_effect = None
    assert session._auto_approve(params)['outcome']['outcome'] == 'cancelled'


def test_kimi_permission_unchanged():
    tc = {'title': 'Bash', 'rawInput': {'command': 'ls'}}
    assert ac.normalize_permission_tool('kimi', tc) is tc


@pytest.mark.parametrize('runtime', ['claude-code', 'codex'])
def test_real_permission_redline_and_roots(runtime, tmp_path):
    session = ac.ACPSession.__new__(ac.ACPSession)
    session.runtime_id = runtime
    session.req_id = 1
    session.role = 'dev'
    session.readonly = False
    session.workdir = tmp_path
    session.main_ws_roots = [tmp_path / 'main']
    session.proc = mock.Mock(pid=987654)
    options = [{'kind': 'allow_once', 'optionId': 'once'}]
    for command in ('pkill kimi', 'pkill claude', 'pkill codex', 'kill 987654',
                    f'rm -rf {tmp_path / "main"}'):
        result = session._auto_approve({'toolCall': {'rawInput': {'command': command}}, 'options': options})
        assert result['outcome']['outcome'] == 'cancelled'
    result = session._auto_approve({'toolCall': {'rawInput': {'command': 'echo "kill codex"'}}, 'options': options})
    assert result['outcome']['optionId'] == 'once'


def test_adapter_close_cleans_owned_process_group(tmp_path, monkeypatch):
    """npx 父进程退出时仍须关闭后代管道，否则 close 会一直等待 reader 锁。"""
    import sys
    import time
    script = tmp_path / 'adapter.py'
    marker = tmp_path / 'ready'
    script.write_text('import subprocess,time\nfrom pathlib import Path\n'
                      'subprocess.Popen(["sleep", "60"])\n'
                      f'Path({str(marker)!r}).write_text("ready")\n'
                      'time.sleep(60)\n')
    monkeypatch.setenv('LOOP_AGENT_CMD_CODEX', ac.shlex.join([sys.executable, str(script)]))
    with open(os.devnull, 'w') as logf:
        session = ac.ACPSession(1, tmp_path, logf, 'dev', tmp_path, runtime='codex')
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            assert marker.exists()
        finally:
            session.close()
        assert session.proc.poll() is not None


def test_codex_patch_checks_every_source_and_move_target(tmp_path):
    session = ac.ACPSession.__new__(ac.ACPSession)
    session.runtime_id = 'codex'
    session.workdir = tmp_path
    session.artifact_root = tmp_path / 'artifacts'
    session.write_roots = [tmp_path]
    options = [{'kind': 'allow_once', 'optionId': 'once'}]
    def approval(changes):
        return session._auto_approve({'options': options, 'toolCall': {
            'title': 'Edit safe.txt', 'rawInput': {'changes': changes}}})['outcome']
    assert approval({'safe.txt': {'type': 'add', 'content': 'x'}})['optionId'] == 'once'
    assert approval({'safe.txt': {'type': 'update', 'move_path': '../escape'}})['outcome'] == 'cancelled'
    assert approval({'safe.txt': {'type': 'add'}, '/etc/passwd': {'type': 'delete'}})['outcome'] == 'cancelled'
    assert approval({'../escape': {'type': 'delete'}})['outcome'] == 'cancelled'
    (tmp_path/'link').symlink_to(tmp_path.parent, target_is_directory=True)
    assert approval({'link/escape': {'type': 'add'}})['outcome'] == 'cancelled'


@pytest.mark.parametrize('runtime', ['claude-code', 'codex'])
def test_start_skips_adapter_authenticate_and_kimi_config(runtime, tmp_path):
    session = ac.ACPSession.__new__(ac.ACPSession)
    session.runtime_id = runtime
    session.tool_profile = None
    session.role = 'dev'
    session.readonly = False
    session._extra_mcp_servers = []
    session.request = mock.Mock(side_effect=[{'authMethods': [{'id': 'external-login'}]},
                                            {'sessionId': 'test-id', 'models': {'currentModelId': 'adapter-model'}}])
    session.start(tmp_path)
    assert [c.args[0] for c in session.request.call_args_list] == ['initialize', 'session/new']
    assert session.model_name == 'adapter-model'


def test_runtime_label_uses_shared_role_capabilities(runtime_config, monkeypatch):
    from board_modules.services.presentation import _effective_model_label
    monkeypatch.setenv('LOOP_AGENT_RUNTIME', 'codex')
    assert _effective_model_label('dev') == 'Codex (codex) · 适配器默认模型'
    assert '受限角色暂不可用' in _effective_model_label('analysis')
    monkeypatch.setenv('LOOP_AGENT_RUNTIME', 'typo')
    assert '运行体配置错误' in _effective_model_label('dev')
