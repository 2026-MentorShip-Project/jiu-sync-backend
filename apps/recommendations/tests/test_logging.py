"""結構化 JSON log(design.md D11、spec「推薦事件輸出結構化 log」)。

見 openspec/changes/add-ai-restaurant-recommendation/tasks.md 1B.1。

兩個 seam:
- `JsonFormatter.format(record)`:直接餵 `logging.LogRecord`,驗證輸出字串。
- Django 設定好的 `apps.recommendations` logger:驗證實際寫到 stdout 的是 JSON,
  而其他 logger(`apps.events`、`config.exceptions`)維持原本行為。

注意:`apps.recommendations` 設 `propagate: False`,pytest 的 `caplog` 掛在 root
logger 上抓不到它,所以這裡用 `capsys` 讀 stdout,不用 `caplog`。
"""

import json
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime
from decimal import Decimal

import pytest
from django.conf import settings

from apps.recommendations.logging import LOG_FIELD_WHITELIST, JsonFormatter

SPEC_EVENT_FIELDS = {
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
}


def _record(msg="ai_rec.succeeded", level=logging.INFO, extra=None, exc_info=None):
    logger = logging.getLogger("apps.recommendations.views")
    record = logger.makeRecord(
        logger.name, level, __file__, 1, msg, (), exc_info, extra=extra
    )
    return record


def _format(record):
    output = JsonFormatter().format(record)
    assert "\n" not in output, "每筆 log 必須是單行"
    return output, json.loads(output)


# ---------- seam 1: JsonFormatter.format() ----------


def test_formats_single_line_json_with_fixed_keys():
    record = _record(extra={"event": "ai_rec.succeeded", "user_id": 7})

    _, data = _format(record)

    assert data["level"] == "INFO"
    assert data["logger"] == "apps.recommendations.views"
    assert data["event"] == "ai_rec.succeeded"
    assert data["user_id"] == 7
    ts = datetime.fromisoformat(data["timestamp"])
    assert ts.tzinfo is not None, "timestamp 必須含時區"
    assert abs(ts.timestamp() - record.created) < 0.001


def test_level_reflects_record_level():
    _, data = _format(_record(level=logging.ERROR, extra={"event": "ai_rec.unavailable"}))
    assert data["level"] == "ERROR"


def test_numeric_fields_stay_json_numbers_and_none_is_null():
    record = _record(
        extra={
            "event": "ai_rec.succeeded",
            "latency_ms": 1234,
            "cost_usd": 0.0123,
            "total_tokens": 0,
            "upstream_status": None,
        }
    )

    raw, data = _format(record)

    assert data["latency_ms"] == 1234
    assert isinstance(data["latency_ms"], int)
    assert isinstance(data["cost_usd"], float)
    assert data["total_tokens"] == 0
    assert data["upstream_status"] is None
    assert '"upstream_status": null' in raw


def test_decimal_serialized_as_number_and_uuid_as_string():
    request_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    record = _record(
        extra={"event": "ai_rec.succeeded", "request_id": request_id, "cost_usd": Decimal("0.0125")}
    )

    _, data = _format(record)

    assert data["request_id"] == "12345678-1234-5678-1234-567812345678"
    assert data["cost_usd"] == 0.0125
    assert isinstance(data["cost_usd"], float)


def test_nan_and_infinity_become_null_so_line_stays_valid_json():
    """spec 要求每行可被解析;json.dumps 預設會輸出不合法的 NaN/Infinity。"""
    record = _record(
        extra={
            "event": "ai_rec.succeeded",
            "cost_usd": float("nan"),
            "latency_ms": float("inf"),
            "total_tokens": Decimal("NaN"),
            "used": 3,
        }
    )

    output = JsonFormatter().format(record)
    data = json.loads(output, parse_constant=lambda c: pytest.fail(f"非標準 JSON 常數 {c}"))

    assert data["cost_usd"] is None
    assert data["latency_ms"] is None
    assert data["total_tokens"] is None
    assert data["used"] == 3


def test_unserializable_value_falls_back_to_minimal_json_line():
    """序列化失敗時不可讓整筆事件消失:輸出只含固定欄位 + format_error 的 JSON。"""
    circular = []
    circular.append(circular)
    record = _record(
        level=logging.WARNING,
        extra={"event": "ai_rec.failed", "error_code": "X", "model": circular},
    )

    _, data = _format(record)

    assert data["event"] == "ai_rec.failed"
    assert data["level"] == "WARNING"
    assert data["logger"] == "apps.recommendations.views"
    assert "timestamp" in data
    assert data["format_error"] == "ValueError"
    assert "model" not in data


def test_non_whitelisted_extra_fields_are_dropped():
    record = _record(
        extra={
            "event": "ai_rec.succeeded",
            "user_id": 1,
            "custom_prompt": "SECRET-USER-TEXT",
            "api_key": "pplx-SECRET",
        }
    )

    raw, data = _format(record)

    assert "custom_prompt" not in data
    assert "api_key" not in data
    assert "SECRET" not in raw


def test_whitelisted_fields_absent_from_extra_are_not_emitted():
    _, data = _format(_record(extra={"event": "ai_rec.in_progress_denied", "user_id": 1}))

    assert set(data) == {"timestamp", "level", "logger", "event", "user_id"}


def test_standard_logrecord_attributes_are_not_leaked():
    record = _record(extra={"event": "ai_rec.succeeded"})

    _, data = _format(record)

    for attr in ("msg", "args", "pathname", "lineno", "funcName", "process", "thread"):
        assert attr not in data


def test_message_args_are_not_emitted():
    """呼叫端若誤把使用者文字放進 %s args,也不應出現在輸出裡(只輸出白名單)。"""
    logger = logging.getLogger("apps.recommendations.views")
    record = logger.makeRecord(
        logger.name, logging.INFO, __file__, 1, "ai_rec.failed %s", ("USER-TEXT",), None,
        extra={"event": "ai_rec.failed"},
    )

    raw, data = _format(record)

    assert "USER-TEXT" not in raw
    assert data["event"] == "ai_rec.failed"


def test_missing_event_extra_outputs_null_event():
    _, data = _format(_record(msg="something happened"))

    assert data["event"] is None


def test_exc_info_adds_exception_fields():
    try:
        raise TimeoutError("upstream took too long")
    except TimeoutError:
        record = _record(
            level=logging.WARNING,
            extra={"event": "ai_rec.failed", "error_code": "AI_RECOMMENDATION_UPSTREAM_TIMEOUT"},
            exc_info=sys.exc_info(),
        )

    _, data = _format(record)

    assert data["exc_type"] == "TimeoutError"
    assert data["exc_message"] == "upstream took too long"
    assert "Traceback (most recent call last)" in data["traceback"]
    assert "TimeoutError: upstream took too long" in data["traceback"]
    assert data["error_code"] == "AI_RECOMMENDATION_UPSTREAM_TIMEOUT"


def test_no_exception_fields_without_exc_info():
    _, data = _format(_record(extra={"event": "ai_rec.succeeded"}))

    assert not {"exc_type", "exc_message", "traceback"} & set(data)


def test_non_ascii_values_are_not_escaped():
    raw, data = _format(_record(extra={"event": "ai_rec.succeeded", "model": "模型-中文"}))

    assert "模型-中文" in raw
    assert "\\u" not in raw
    assert data["model"] == "模型-中文"


def test_whitelist_matches_spec_event_fields():
    assert set(LOG_FIELD_WHITELIST) == SPEC_EVENT_FIELDS


def test_fixed_keys_cannot_be_overridden_by_extra():
    """白名單欄位名稱不含固定欄位;即使 extra 帶 level/timestamp 也不會覆蓋固定值。"""
    record = _record(extra={"event": "ai_rec.succeeded", "level": "FAKE", "timestamp": "fake"})

    _, data = _format(record)

    assert data["timestamp"] != "fake"
    assert data["level"] == "INFO"


# ---------- seam 2: Django LOGGING 設定 ----------


def test_recommendations_logger_writes_json_to_stdout(capsys):
    logging.getLogger("apps.recommendations.views").info(
        "ai_rec.quota_denied",
        extra={
            "event": "ai_rec.quota_denied",
            "user_id": 3,
            "used": 20,
            "limit": 20,
            "period": "2026-09",
        },
    )

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["event"] == "ai_rec.quota_denied"
    assert data["logger"] == "apps.recommendations.views"
    assert data["used"] == 20
    assert data["period"] == "2026-09"


def test_recommendations_logger_emits_info_level(capsys):
    logging.getLogger("apps.recommendations").info("x", extra={"event": "ai_rec.succeeded"})
    assert capsys.readouterr().out.strip()


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_recommendations_logger_does_not_propagate_to_root():
    # 不用 caplog 驗證:pytest 9 會把 caplog handler 也掛到 propagate=False 的 logger
    # 上,caplog 看得到不代表有傳到 root。這裡直接在 root 掛 handler。
    root = logging.getLogger()
    handler = _ListHandler()
    root.addHandler(handler)
    try:
        logging.getLogger("apps.recommendations.views").info(
            "ai_rec.succeeded", extra={"event": "ai_rec.succeeded"}
        )
    finally:
        root.removeHandler(handler)

    assert handler.records == []


def test_other_loggers_keep_default_behavior(caplog, capsys):
    events_logger = logging.getLogger("apps.events.views")
    exceptions_logger = logging.getLogger("config.exceptions")

    with caplog.at_level(logging.WARNING):
        events_logger.warning("event warning %s", "abc")
        exceptions_logger.error("API error response: %s", "GET")

    messages = [(r.name, r.getMessage()) for r in caplog.records]
    assert ("apps.events.views", "event warning abc") in messages
    assert ("config.exceptions", "API error response: GET") in messages
    # 其他 logger 沒有被掛上 JSON handler,stdout 不會出現 JSON 行
    assert "event warning abc" not in capsys.readouterr().out
    for name in ("apps.events", "apps.events.views", "config.exceptions", "config"):
        assert logging.getLogger(name).handlers == []


_DISABLE_PROBE = """
import logging
import django
existing = [logging.getLogger("apps.events.views"), logging.getLogger("config.exceptions")]
django.setup()  # 依 settings.LOGGING 呼叫 dictConfig
print([logger.disabled for logger in existing])
"""


def test_django_setup_does_not_disable_loggers_created_before_it():
    """disable_existing_loggers 必須為 False:在 Django 套用 LOGGING 之前就已建立的
    logger 不可被停用。放在子行程跑,避免在測試行程內重跑 dictConfig 汙染全域 logging。
    """
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.dev"}
    result = subprocess.run(
        [sys.executable, "-c", _DISABLE_PROBE],
        capture_output=True,
        text=True,
        env=env,
        cwd=settings.BASE_DIR,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "[False, False]"


def test_logging_setting_only_configures_recommendations_logger():
    assert settings.LOGGING["disable_existing_loggers"] is False
    assert set(settings.LOGGING["loggers"]) == {"apps.recommendations"}
    cfg = settings.LOGGING["loggers"]["apps.recommendations"]
    assert cfg["level"] == "INFO"
    assert cfg["propagate"] is False
    assert "root" not in settings.LOGGING
