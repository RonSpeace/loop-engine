"""#32 Phase 6：board.py 路由蓝图包（卡 #39）。ALL 即注册顺序，board.py 只按它注册。"""
from board_modules.blueprints import (  # noqa: F401
    requirements,
    projects,
    chats,
    settings,
    assistant,
    sessions,
    meta,
    skills,
)

ALL = (requirements, projects, chats, settings, assistant, sessions, meta, skills, )
