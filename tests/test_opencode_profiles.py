"""特殊聊天档位必须在 OpenCode 原生权限层约束，不能只依赖角色提示词。"""
import pytest
import acp_shared as ac


@pytest.mark.parametrize('flags', [dict(deny_builtin_write=True), dict(allow_fs_write=False)])
def test_special_write_denial_applies_without_readonly_role(flags):
    permission = ac.opencode_session_config(readonly=False, role='dev', **flags)['permission']
    assert permission['edit'] == permission['write'] == permission['patch'] == 'deny'
    assert permission['bash']['*'] == 'deny'
    assert permission['bash']['ls'] == 'ask'
    assert permission['task'] == permission['skill'] == permission['webfetch'] == 'deny'


def test_assistant_defaults_to_deny_and_only_allows_declared_mcp():
    permission = ac.opencode_session_config(tool_profile='feishu_assistant', extra_mcp_servers=[
        {'name': 'reqboard-assistant'}, {'name': '*'}, {'name': ''}])['permission']
    assert permission == {'*': 'deny', 'reqboard-assistant_*': 'allow'}
