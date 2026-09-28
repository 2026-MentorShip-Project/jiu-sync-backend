"""`apps.recommendations` 專用的結構化 JSON log formatter(design.md D11)。

每筆 record 輸出一行 JSON,供 Loki 依欄位過濾與計算指標。只輸出固定欄位
(`timestamp`/`level`/`logger`/`event`)與 `LOG_FIELD_WHITELIST` 內、呼叫端透過
`extra` 帶入的欄位;訊息本文與 `%s` args 一律不輸出——避免之後有人把 prompt、
使用者自由文字、上游原始回應或 API key 塞進 log 就直接被印出。

呼叫方式:
    logger.info("ai_rec.succeeded", extra={"event": "ai_rec.succeeded", "latency_ms": 1234, ...})
"""

import json
import logging
import math
import sys
from datetime import UTC, datetime
from decimal import Decimal

# spec「推薦事件輸出結構化 log」列出的所有事件欄位的聯集。新增欄位時只改這裡。
LOG_FIELD_WHITELIST = frozenset(
    {
        "request_id",
        "user_id",
        "event_id",
        "latency_ms",
        "restaurant_count",
        "total_tokens",
        "cost_usd",
        "model",
        "error_code",
        "upstream_status",
        "used",
        "limit",
        "period",
        "record_missing",
        "model_fallback",
    }
)


_FIXED_KEYS = ("timestamp", "level", "logger", "event")


def _json_safe(value):
    """轉成可被標準 JSON 解析的值:Decimal 維持數字、NaN/Infinity 轉 null
    (`json.dumps` 預設會輸出不合法的 `NaN`)。其他型別交給 `_json_default`。"""
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _json_default(value):
    # 非 JSON 原生型別(UUID、datetime 等)轉字串。
    return str(value)


class JsonFormatter(logging.Formatter):
    """每筆 record 輸出單行 JSON:固定欄位 + 白名單 `extra` 欄位 + 例外資訊。

    序列化失敗時(例如循環參照)改輸出只含固定欄位與 `format_error` 的 JSON 行,
    不讓整筆事件消失,也不把失敗吞掉。
    """

    def format(self, record):
        data = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", None),
        }
        for field in sorted(LOG_FIELD_WHITELIST):
            if field in record.__dict__:
                data[field] = _json_safe(record.__dict__[field])
        if record.exc_info and record.exc_info[0] is not None:
            exc_type, exc_value, _ = record.exc_info
            data["exc_type"] = exc_type.__name__
            data["exc_message"] = str(exc_value)
            data["traceback"] = self.formatException(record.exc_info)
        try:
            return json.dumps(data, ensure_ascii=False, allow_nan=False, default=_json_default)
        except (TypeError, ValueError) as exc:
            minimal = {key: data[key] for key in _FIXED_KEYS}
            minimal["format_error"] = type(exc).__name__
            return json.dumps(minimal, ensure_ascii=False, default=_json_default)


class StdoutStreamHandler(logging.StreamHandler):
    """每次輸出時才取當下的 `sys.stdout`。

    `ext://sys.stdout` 會在 `dictConfig` 當下綁死 stream 物件;若之後 `sys.stdout`
    被替換(pytest `capsys`、部分 process manager 的重導),log 會寫到舊的 stream。
    """

    def __init__(self):
        super().__init__(sys.stdout)

    def emit(self, record):
        self.stream = sys.stdout
        super().emit(record)
