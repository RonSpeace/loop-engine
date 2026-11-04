"""存储时间的唯一口径；无配置时使用操作系统本地时区（包括该日期的夏令时）。"""
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def configured_timezone(value):
    """空值代表系统本地时区；非法配置拒绝启动。"""
    value = (value or "").strip()
    if not value:
        return None
    match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", value)
    if match:
        hours, minutes = int(match[2]), int(match[3])
        if hours > 23 or minutes > 59:
            raise ValueError(f"REQ_BOARD_TZ 非法: {value!r}")
        delta = timedelta(hours=hours, minutes=minutes)
        return timezone(delta if match[1] == "+" else -delta)
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"REQ_BOARD_TZ 非法: {value!r}（需为 IANA 时区或 ±HH:MM）") from exc


STORAGE_TZ = configured_timezone(os.environ.get("REQ_BOARD_TZ"))


def storage_datetime(value):
    """给无时区时间补存储时区；系统本地模式按目标日期换算，不能固定当前 UTC 偏移。"""
    if value.tzinfo is not None:
        return value.astimezone(STORAGE_TZ)
    return value.replace(tzinfo=STORAGE_TZ) if STORAGE_TZ else value.astimezone()


def parse_storage_time(value):
    """解析数据库/冻结时钟的墙钟字符串。"""
    return storage_datetime(datetime.strptime(str(value), TIME_FORMAT))


def format_storage_time(epoch=None):
    """epoch 秒（省略为现在）→ 数据库存储字符串。"""
    value = datetime.now(STORAGE_TZ) if epoch is None else datetime.fromtimestamp(epoch, STORAGE_TZ)
    return value.strftime(TIME_FORMAT)
