"""經 PgBouncer 跑 pytest 時的測試資料庫清理(add-pgbouncer-and-celery-worker
design.md D4 Q6)。

PgBouncer 會保留對測試資料庫的閒置 server 連線,pytest-django 收尾的
DROP DATABASE 因此失敗。根目錄 conftest.py 在 DROP 前呼叫
release_pgbouncer_server_connections():有設定 PGBOUNCER_ADMIN_URL 時對
PgBouncer 管理資料庫下 KILL 切斷連線,再 RESUME(KILL 後該資料庫會維持暫停,
不 RESUME 下一次連線會卡住,task 2.1 實測);未設定時完全不作用。
"""

import psycopg
import pytest

from config.pgbouncer_cleanup import release_pgbouncer_server_connections

ADMIN_URL = "postgres://jiu_sync:jiu_sync@localhost:6433/pgbouncer"


class FakeAdminConnection:
    def __init__(self, fail_on=None):
        self.executed = []
        self.closed = False
        self._fail_on = fail_on

    def execute(self, query):
        self.executed.append(query)
        if self._fail_on is not None and query.startswith(self._fail_on):
            raise psycopg.errors.ProtocolViolation(f"{self._fail_on} failed")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True
        return False


class FakeConnect:
    def __init__(self, connection=None):
        self.calls = []
        self.connection = connection or FakeAdminConnection()

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.connection


def test_does_nothing_when_admin_url_not_set():
    connect = FakeConnect()

    release_pgbouncer_server_connections("test_jiu_sync", environ={}, connect=connect)

    assert connect.calls == []


def test_does_nothing_when_admin_url_is_empty():
    """邊界:.env 留了 `PGBOUNCER_ADMIN_URL=` 空值,視同未設定(直連開發不受影響)。"""
    connect = FakeConnect()

    release_pgbouncer_server_connections(
        "test_jiu_sync", environ={"PGBOUNCER_ADMIN_URL": ""}, connect=connect
    )

    assert connect.calls == []


def test_kills_then_resumes_test_database_on_admin_console():
    connect = FakeConnect()

    release_pgbouncer_server_connections(
        "test_jiu_sync", environ={"PGBOUNCER_ADMIN_URL": ADMIN_URL}, connect=connect
    )

    [(args, kwargs)] = connect.calls
    assert args == (ADMIN_URL,)
    assert kwargs["autocommit"] is True
    assert connect.connection.executed == ['KILL "test_jiu_sync"', 'RESUME "test_jiu_sync"']
    assert connect.connection.closed is True


def test_admin_connection_has_bounded_timeout():
    """CLAUDE.md 資料操作穩健性 7:PGBOUNCER_ADMIN_URL 指到不可達主機時不可無限等待。"""
    connect = FakeConnect()

    release_pgbouncer_server_connections(
        "test_jiu_sync", environ={"PGBOUNCER_ADMIN_URL": ADMIN_URL}, connect=connect
    )

    [(_, kwargs)] = connect.calls
    assert 0 < kwargs["connect_timeout"] <= 10


def test_logs_kill_and_resume(caplog):
    """CLAUDE.md 資料操作穩健性 8:清理有沒有執行要能從 log 查到。"""
    connect = FakeConnect()

    with caplog.at_level("INFO", logger="config.pgbouncer_cleanup"):
        release_pgbouncer_server_connections(
            "test_jiu_sync", environ={"PGBOUNCER_ADMIN_URL": ADMIN_URL}, connect=connect
        )

    assert "test_jiu_sync" in caplog.text
    assert "KILL" in caplog.text and "RESUME" in caplog.text


def test_quotes_database_name_as_identifier():
    """邊界:名稱含雙引號時需跳脫,避免組出錯誤/被注入的管理指令。"""
    connect = FakeConnect()

    release_pgbouncer_server_connections(
        'test_"x', environ={"PGBOUNCER_ADMIN_URL": ADMIN_URL}, connect=connect
    )

    assert connect.connection.executed == ['KILL "test_""x"', 'RESUME "test_""x"']


def test_kill_failure_is_raised_not_swallowed():
    connect = FakeConnect(FakeAdminConnection(fail_on="KILL"))

    with pytest.raises(psycopg.Error, match="KILL failed"):
        release_pgbouncer_server_connections(
            "test_jiu_sync", environ={"PGBOUNCER_ADMIN_URL": ADMIN_URL}, connect=connect
        )

    assert connect.connection.executed == ['KILL "test_jiu_sync"']
    assert connect.connection.closed is True


def test_resume_failure_is_raised_not_swallowed():
    connect = FakeConnect(FakeAdminConnection(fail_on="RESUME"))

    with pytest.raises(psycopg.Error, match="RESUME failed"):
        release_pgbouncer_server_connections(
            "test_jiu_sync", environ={"PGBOUNCER_ADMIN_URL": ADMIN_URL}, connect=connect
        )


def test_admin_connection_failure_is_raised():
    """錯誤輸入:PGBOUNCER_ADMIN_URL 指錯(PgBouncer 未啟動)時明確失敗。"""

    def refusing_connect(*args, **kwargs):
        raise psycopg.OperationalError("connection refused")

    with pytest.raises(psycopg.OperationalError):
        release_pgbouncer_server_connections(
            "test_jiu_sync",
            environ={"PGBOUNCER_ADMIN_URL": ADMIN_URL},
            connect=refusing_connect,
        )


def test_empty_database_name_is_rejected():
    connect = FakeConnect()

    with pytest.raises(ValueError):
        release_pgbouncer_server_connections(
            "", environ={"PGBOUNCER_ADMIN_URL": ADMIN_URL}, connect=connect
        )

    assert connect.calls == []
