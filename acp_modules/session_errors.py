"""聊天服务共用的运行体启动错误提示。"""


def chat_session_error(error, project=None):
    """三种聊天服务共用启动错误文案，不把底层异常与路径交给用户。"""
    from acp_shared import role_runtime
    try:
        runtime = role_runtime("analysis", project)
    except ValueError:
        import os
        runtime = os.environ.get("LOOP_AGENT_RUNTIME", "kimi")
    if isinstance(error, FileNotFoundError):
        return f"当前运行体 {runtime} 未安装，请去 设置→模型 配置可用运行体"
    if isinstance(error, ValueError) and any(word in str(error) for word in ("运行体", "runtime", "LOOP_AGENT_CMD")):
        return f"当前运行体 {runtime} 不支持聊天会话，请去 设置→模型 配置可用运行体"
    return f"聊天会话启动失败，请检查 设置→模型 的运行体和模型配置（{runtime}）"
