"""App 全面結構化 log(add-observability-stack design.md D11、tasks.md 4.3)。

三個 seam:
- `AppJsonFormatter.format(record)`:直接餵 `logging.LogRecord`,驗證輸出字串。
- Django 設定好的 `apps` / `config` logger:驗證實際寫到 stdout 的是 JSON、不傳到 root,
  且 `apps.recommendations` 的白名單 JSON 不受影響。
- `log_event_on_commit()`:狀態變更的事件 log 只在交易 commit 後輸出。
"""

import json
import logging
import uuid
from decimal import Decimal

import pytest
from django.conf import settings
from django.db import transaction

from config.logging import APP_LOG_FIELD_WHITELIST, AppJsonFormatter, log_event_on_commit

FIXED_KEYS = {"timestamp", "level", "logger", "message", "event"}


def _record(msg="event created", args=(), level=logging.INFO, extra=None, exc_info=None):
    logger = logging.getLogger("apps.events.views")
    return logger.makeRecord(logger.name, level, __file__, 1, msg, args, exc_info, extra=extra)


def _format(record):
    output = AppJsonFormatter().format(record)
    assert "\n" not in output, "每筆 log 必須是單行"
    return json.loads(output)


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


# ---------- seam 1: AppJsonFormatter.format() ----------


def test_formats_single_line_json_with_fixed_keys_and_message():
    data = _format(_record(extra={"event": "event.created"}))

    assert FIXED_KEYS <= set(data)
    assert data["level"] == "INFO"
    assert data["logger"] == "apps.events.views"
    assert data["message"] == "event created"
    assert data["event"] == "event.created"
    assert data["timestamp"].endswith("+00:00")


def test_message_args_are_interpolated():
    data = _format(_record(msg="API error response: %s %s status=%s", args=("GET", "/x", 500)))

    assert data["message"] == "API error response: GET /x status=500"


def test_multiline_message_still_outputs_single_line():
    data = _format(_record(msg="line1\nline2"))

    assert data["message"] == "line1\nline2"


def test_missing_event_extra_outputs_null_event():
    assert _format(_record())["event"] is None


@pytest.mark.parametrize("level", [logging.DEBUG, logging.WARNING, logging.ERROR, logging.CRITICAL])
def test_level_reflects_record_level(level):
    assert _format(_record(level=level))["level"] == logging.getLevelName(level)


def test_whitelisted_extra_fields_are_emitted():
    user_id = uuid.uuid4()
    data = _format(
        _record(
            extra={
                "event": "notification.sent",
                "user_id": user_id,
                "event_id": "AbCd1234",
                "response_id": "Zz9Yy8Xx",
                "task": "send_event_finalized_email",
                "reason": "stale",
                "recipient_count": 3,
                "is_new_user": True,
            }
        )
    )

    assert data["user_id"] == str(user_id)
    assert data["event_id"] == "AbCd1234"
    assert data["response_id"] == "Zz9Yy8Xx"
    assert data["task"] == "send_event_finalized_email"
    assert data["reason"] == "stale"
    assert data["recipient_count"] == 3
    assert data["is_new_user"] is True


def test_whitelist_matches_design():
    assert APP_LOG_FIELD_WHITELIST == {
        "user_id",
        "event_id",
        "response_id",
        "task",
        "reason",
        "recipient_count",
        "is_new_user",
    }


def test_non_whitelisted_extra_fields_are_dropped():
    data = _format(_record(extra={"email": "a@example.com", "title": "秘密聚會", "token": "t"}))

    assert "email" not in data
    assert "title" not in data
    assert "token" not in data


def test_standard_logrecord_attributes_are_not_leaked():
    data = _format(_record())

    for attr in ("args", "msg", "pathname", "lineno", "funcName", "process", "thread"):
        assert attr not in data


def test_whitelisted_fields_absent_from_extra_are_not_emitted():
    data = _format(_record())

    assert set(data) == FIXED_KEYS


def test_decimal_and_non_finite_values_stay_valid_json():
    data = _format(_record(extra={"recipient_count": Decimal("2"), "reason": float("nan")}))

    assert data["recipient_count"] == 2
    assert data["reason"] is None


def test_non_ascii_message_is_not_escaped():
    output = AppJsonFormatter().format(_record(msg="活動已建立"))

    assert "活動已建立" in output


def test_exc_info_adds_exception_fields():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        exc_info = sys.exc_info()

    data = _format(_record(level=logging.ERROR, exc_info=exc_info))

    assert data["exc_type"] == "ValueError"
    assert data["exc_message"] == "boom"
    assert "Traceback" in data["traceback"]


def test_no_exception_fields_without_exc_info():
    data = _format(_record())

    assert "exc_type" not in data
    assert "traceback" not in data


def test_unserializable_value_falls_back_to_minimal_json_line():
    circular = []
    circular.append(circular)

    data = _format(_record(extra={"event": "event.created", "reason": circular}))

    assert data["event"] == "event.created"
    assert data["message"] == "event created"
    assert data["format_error"] == "ValueError"
    assert "reason" not in data


def test_message_that_fails_to_interpolate_does_not_drop_the_line():
    # args 數量不符時 getMessage() 會拋 TypeError;仍要輸出一行可解析的 JSON。
    data = _format(_record(msg="%s %s", args=("only-one",)))

    assert data["message"] == "%s %s"
    assert data["format_error"] == "TypeError"


# ---------- seam 2: Django LOGGING 設定 ----------


def test_logging_setting_configures_apps_and_config_loggers():
    loggers = settings.LOGGING["loggers"]
    assert settings.LOGGING["disable_existing_loggers"] is False
    assert set(loggers) == {"apps", "config", "apps.recommendations"}
    for name in ("apps", "config"):
        assert loggers[name]["level"] == "INFO"
        assert loggers[name]["propagate"] is False
        assert loggers[name]["handlers"] == ["app_stdout"]
    assert "root" not in settings.LOGGING


def test_recommendations_logger_config_is_unchanged():
    cfg = settings.LOGGING["loggers"]["apps.recommendations"]
    assert cfg == {"handlers": ["recommendations_stdout"], "level": "INFO", "propagate": False}


@pytest.mark.parametrize(
    "name",
    ["apps.events.views", "apps.accounts.views", "apps.notifications.tasks", "config.exceptions"],
)
def test_app_logger_info_is_written_as_json_to_stdout(capsys, name):
    logging.getLogger(name).info("hello %s", "world", extra={"event": "x.y"})

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["logger"] == name
    assert data["message"] == "hello world"
    assert data["event"] == "x.y"
    assert data["level"] == "INFO"


def test_app_logger_debug_is_not_written(capsys):
    logging.getLogger("apps.events.views").debug("noise")

    assert capsys.readouterr().out.strip() == ""


def test_config_exceptions_warning_is_json_with_message(capsys):
    logging.getLogger("config.exceptions").warning(
        "API error response: %s %s status=%s", "GET", "/api/x", 401
    )

    data = json.loads(capsys.readouterr().out.strip())
    assert data["level"] == "WARNING"
    assert data["message"] == "API error response: GET /api/x status=401"


@pytest.mark.parametrize("name", ["apps.events.views", "config.exceptions"])
def test_app_loggers_do_not_propagate_to_root(name):
    root = logging.getLogger()
    handler = _ListHandler()
    root.addHandler(handler)
    try:
        logging.getLogger(name).warning("x")
    finally:
        root.removeHandler(handler)

    assert handler.records == []


def test_recommendations_logger_still_whitelist_only_and_not_duplicated(capsys):
    logging.getLogger("apps.recommendations.views").info(
        "ai_rec.succeeded secret-prompt", extra={"event": "ai_rec.succeeded", "latency_ms": 5}
    )

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["event"] == "ai_rec.succeeded"
    assert "message" not in data
    assert "secret-prompt" not in lines[0]


@pytest.mark.parametrize("name", ["httpx", "urllib3", "celery", "django"])
def test_third_party_loggers_are_not_configured(name):
    assert name not in settings.LOGGING["loggers"]


# ---------- seam 3: log_event_on_commit() ----------

_logger = logging.getLogger("apps.events.views")


def _events(caplog, name):
    return [r for r in caplog.records if getattr(r, "event", None) == name]


@pytest.mark.django_db
def test_log_event_on_commit_logs_after_commit(caplog, django_capture_on_commit_callbacks):
    caplog.set_level(logging.INFO)
    with django_capture_on_commit_callbacks(execute=True):
        log_event_on_commit(_logger, "event.created", "event created", event_id="AbCd1234")
        assert _events(caplog, "event.created") == []

    records = _events(caplog, "event.created")
    assert len(records) == 1
    assert records[0].event_id == "AbCd1234"
    assert records[0].getMessage() == "event created"
    assert records[0].levelno == logging.INFO


@pytest.mark.django_db
def test_log_event_on_commit_skips_when_transaction_rolls_back(
    caplog, django_capture_on_commit_callbacks
):
    caplog.set_level(logging.INFO)
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError):
            with transaction.atomic():
                log_event_on_commit(_logger, "event.created", "event created", event_id="x")
                raise RuntimeError("rollback")

    assert _events(caplog, "event.created") == []


@pytest.mark.django_db(transaction=True)
def test_log_event_on_commit_logs_immediately_outside_atomic(caplog):
    caplog.set_level(logging.INFO)

    log_event_on_commit(_logger, "auth.logout", "logout", user_id="u1")

    assert len(_events(caplog, "auth.logout")) == 1


def test_log_event_on_commit_rejects_non_whitelisted_field():
    with pytest.raises(ValueError, match="email"):
        log_event_on_commit(_logger, "event.created", "event created", email="a@example.com")


def test_celery_worker_does_not_redirect_stdout():
    # Celery worker 預設把 sys.stdout 換成 LoggingProxy;StdoutStreamHandler 每次取當下
    # 的 sys.stdout,會讓通知信 task 的 JSON 被包上 "[... WARNING/ForkPoolWorker-1]"
    # 前綴,Loki 的 `| json` 無法解析(design.md D11)。
    from config.celery import app

    assert settings.CELERY_WORKER_REDIRECT_STDOUTS is False
    assert app.conf.worker_redirect_stdouts is False
