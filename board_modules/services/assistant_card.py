"""#32 Phase 5：assistant_card 服务（自 board.py 原样搬迁，逻辑不变）。

补丁名单名字与留在 board.py 的名字一律经 runtime.resolve 在调用期读取。
"""

from board_modules.runtime import resolve
# ---- #30 需求卡清单卡片：分页预算（分页必须在落库前——_assistant_add_event 走 [:上限] 截断，
# 截断后的 JSON 非法，发送端补救不了；页内长度上限 < ASSISTANT_EVENT_MAX 留余量）----
ASSISTANT_CARD_MAX_ITEMS = 20      # 单页最多需求卡数
ASSISTANT_CARD_MAX_PAGES = 10      # 单次清单最多页数（= 200 张）；超出末页显式 truncated + remaining
ASSISTANT_CARD_PAGE_MAX = 3000     # 单页事件序列化长度上限（< ASSISTANT_EVENT_MAX 4000）
ASSISTANT_CARD_FLOW_FALLBACK = "未指定"   # flow_name 空/NULL（存量卡）兜底文案


def _card_page_payload(project, items, page, pages, total_cards, truncated, remaining):
    """#30 一页需求卡清单的页 spec（**服务端确定性生成**，版本化 v:1；dispatcher 侧据此渲染卡片）。
    字段全部来自服务端：status_label 取 STATUS_LABEL_CN（纯中文）、flow 取 flow_name 快照或「未指定」。"""
    return {"v": 1, "type": "requirement_list", "project": project, "page": page, "pages": pages,
            "total_cards": total_cards, "truncated": bool(truncated),
            "remaining": max(0, int(remaining)), "items": items}


def _assistant_add_card_event(sid, payload_text):
    """#30 写一条 kind='card' 事件（payload_text 为已序列化的页 spec；长度断言在调用方）。"""
    return resolve("_assistant_add_event")(sid, "card", payload_text)
