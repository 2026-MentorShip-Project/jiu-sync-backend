"""`apps.*` / `config.*` 共用的結構化 JSON log(add-observability-stack design.md D11)。

每筆 record 輸出一行 JSON:`timestamp`/`level`/`logger`/`message`/`event`,加上
`APP_LOG_FIELD_WHITELIST` 內、呼叫端透過 `extra` 帶入的欄位,以及例外資訊。與
`apps.recommendations.logging.JsonFormatter` 不同,這裡會輸出訊息本文,所以呼叫端
SHALL NOT 把 email、活動標題、留言內容或 token 放進訊息或 extra——一律用 id。

狀態變更的事件 log 用 `log_event_on_commit()`:只在交易 commit 後輸出。
"""

import json
import logging
from datetime import UTC, datetime

from django.db import transaction

from apps.recommendations.logging import _json_default, _json_safe

APP_LOG_FIELD_WHITELIST = frozenset(
    {
        "user_id",
        "event_id",
        "response_id",
        "task",
        "reason",
        "recipient_count",
        "is_new_user",
    }
)

_FIXED_KEYS = ("timestamp", "level", "logger", "message", "event")


class AppJsonFormatter(logging.Formatter):
    """每筆 record 輸出單行 JSON。訊息插值或序列化失敗時改輸出只含固定欄位與
    `format_error` 的一行,不讓整筆事件消失,也不把失敗吞掉。"""

    def format(self, record):
        format_error = None
        try:
            message = record.getMessage()
        except (TypeError, ValueError) as exc:
            message = str(record.msg)
            format_error = type(exc).__name__

        data = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
            "event": getattr(record, "event", None),
        }
        if format_error is not None:
            data["format_error"] = format_error
        for field in sorted(APP_LOG_FIELD_WHITELIST):
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


def log_event_on_commit(logger, event, message, **fields):
    """交易 commit 後才以 INFO 輸出事件 log;rollback 時不輸出;不在 atomic 內時立即輸出。

    `fields` 只接受 `APP_LOG_FIELD_WHITELIST` 內的欄位,其他欄位在呼叫當下就拋
    `ValueError`——formatter 會靜默丟掉白名單外的欄位,這裡提早擋下打錯字或誤傳個資。
    """
    unknown = set(fields) - APP_LOG_FIELD_WHITELIST
    if unknown:
        raise ValueError(f"log fields not in APP_LOG_FIELD_WHITELIST: {sorted(unknown)}")
    extra = {"event": event, **fields}
    transaction.on_commit(lambda: logger.info(message, extra=extra))
