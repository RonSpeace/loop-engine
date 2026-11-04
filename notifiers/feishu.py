"""飞书渠道：包装调度器既有的飞书发送函数（行为逐字不变）。

飞书是唯一的双向渠道：出站通知走本类（同步、异常原样抛给调用方，与接入注册表前一致）；
入站 y/n 回复、看板助手、建卡会话仍由 dispatcher.on_message 专属处理，不进注册表。
凭据仍读 feishu.json（向后兼容），不在 notifiers.json 里配置。
"""
from __future__ import annotations

from notifiers import CallableNotifier


class FeishuNotifier(CallableNotifier):
    """send_text(text) → 飞书文本；send_card(event, req, **kw) → 飞书需求卡片（含失败降级文本）。"""

    def __init__(self, send_text, send_card):
        super().__init__("feishu",
                         on_text=send_text,
                         on_event=lambda ev: send_card(ev.kind, ev.raw.get("req"), **ev.raw.get("kw", {})),
                         raise_errors=True)
