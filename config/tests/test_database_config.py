"""DATABASES 連線選項(add-pgbouncer-and-celery-worker design.md D1)。

PgBouncer transaction pooling 下 server-side cursor 跨語句失效,必須關閉;
CONN_MAX_AGE 讓 Django 重用到 PgBouncer 的 client 連線,預設 60 秒、可由
`.env` 的 DB_CONN_MAX_AGE 覆寫;CONN_HEALTH_CHECKS 避免重用到已斷的連線。
"""

import pytest
from django.conf import settings

from config.settings.base import _database_config

DEFAULT_URL = "postgres://jiu_sync:jiu_sync@localhost:5455/jiu_sync"


def test_default_database_disables_server_side_cursors():
    assert settings.DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] is True


def test_default_database_conn_max_age_defaults_to_60(monkeypatch):
    monkeypatch.delenv("DB_CONN_MAX_AGE", raising=False)

    assert _database_config()["CONN_MAX_AGE"] == 60


def test_default_database_enables_conn_health_checks():
    assert settings.DATABASES["default"]["CONN_HEALTH_CHECKS"] is True


def test_conn_max_age_can_be_overridden_by_env(monkeypatch):
    monkeypatch.setenv("DB_CONN_MAX_AGE", "120")

    assert _database_config()["CONN_MAX_AGE"] == 120


def test_conn_max_age_zero_disables_persistent_connections(monkeypatch):
    """邊界:0 是合法值(每個請求結束即關閉連線,等同 Django 預設),不可被當成未設定。"""
    monkeypatch.setenv("DB_CONN_MAX_AGE", "0")

    assert _database_config()["CONN_MAX_AGE"] == 0


def test_conn_max_age_invalid_value_raises(monkeypatch):
    """錯誤輸入:設定錯誤要在啟動時明確失敗,不可默默退回預設值。"""
    monkeypatch.setenv("DB_CONN_MAX_AGE", "sixty")

    with pytest.raises(ValueError):
        _database_config()


def test_database_config_keeps_connection_target_from_database_url(monkeypatch):
    """連線目標仍只由 DATABASE_URL 決定(本機預設直連 5455,不因新選項改變)。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    config = _database_config()

    assert (config["HOST"], config["PORT"], config["NAME"]) == ("localhost", 5455, "jiu_sync")
