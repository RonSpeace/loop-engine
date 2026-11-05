"""聊天服务启动失败必须提供操作指引，不回传底层堆栈。"""
import pytest
from unittest import mock
from board_modules.services.chat_runtime import chat_session_error


@pytest.mark.parametrize("runtime", ["kimi", "opencode", "codex", "claude-code"])
@pytest.mark.parametrize("error, expected", [
    (FileNotFoundError("secret binary path"), "未安装"),
    (ValueError("运行体未验证受限工具"), "不支持聊天会话"),
    (RuntimeError("private diagnostic"), "聊天会话启动失败"),
])
def test_chat_session_error(runtime, error, expected):
    with mock.patch("acp_shared.role_runtime", return_value=runtime):
        message = chat_session_error(error, "p")
    assert expected in message
    assert runtime in message
    assert "设置→模型" in message
    assert "private diagnostic" not in message
    assert "secret binary path" not in message

    from scripts.i18n_en_source import PHRASES
    assert "Settings → Models" in PHRASES[message]
