"""`gunicorn.conf.py` 與 Dockerfile `CMD`(add-observability-stack design.md D2)。

- 參數沿用原本 Dockerfile `CMD`:bind 0.0.0.0:8000、gthread、3 workers、4 threads、
  timeout 60、worker_tmp_dir /dev/shm——以 gunicorn 自己的設定載入結果驗證。
- `on_starting` 清空並重建 `PROMETHEUS_MULTIPROC_DIR`(重啟不殘留舊數值);
  `child_exit` 以 worker pid 呼叫 `mark_process_dead`。
- 未設 `PROMETHEUS_MULTIPROC_DIR`(本機直接跑 gunicorn)時兩個 hook 都不做事、不報錯。
"""

import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from gunicorn.app.base import Application

REPO_ROOT = Path(__file__).resolve().parents[2]
GUNICORN_CONF = REPO_ROOT / "gunicorn.conf.py"
DOCKERFILE = REPO_ROOT / "Dockerfile"


def _load_module():
    spec = importlib.util.spec_from_file_location("gunicorn_conf_under_test", GUNICORN_CONF)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def conf():
    return _load_module()


class _ConfigOnlyApp(Application):
    def load_config(self):
        self.load_config_from_file(str(GUNICORN_CONF))

    def load(self):  # pragma: no cover - 不啟動 app
        return None


def test_gunicorn_loads_same_parameters_as_previous_cmd(monkeypatch):
    # WEB_CONCURRENCY 會改變 workers 預設值;確保驗的是設定檔本身。
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.delenv("GUNICORN_CMD_ARGS", raising=False)
    cfg = _ConfigOnlyApp().cfg
    assert cfg.bind == ["0.0.0.0:8000"]
    assert cfg.worker_class_str == "gthread"
    assert cfg.workers == 3
    assert cfg.threads == 4
    assert cfg.timeout == 60
    assert cfg.worker_tmp_dir == "/dev/shm"


def test_dockerfile_cmd_uses_config_file():
    cmd_lines = [
        line for line in DOCKERFILE.read_text().splitlines() if re.match(r"^\s*CMD\b", line)
    ]
    assert len(cmd_lines) == 1
    cmd = json.loads(cmd_lines[0].split("CMD", 1)[1])
    assert cmd == ["gunicorn", "-c", "gunicorn.conf.py", "config.wsgi:application"]


# ---------------------------------------------------------------- on_starting


def test_on_starting_creates_dir_when_missing(conf, tmp_path, monkeypatch):
    target = tmp_path / "nested" / "prometheus"
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(target))
    conf.on_starting(SimpleNamespace())
    assert target.is_dir()
    assert list(target.iterdir()) == []


def test_on_starting_clears_stale_files(conf, tmp_path, monkeypatch):
    target = tmp_path / "prometheus"
    target.mkdir()
    (target / "counter_123.db").write_bytes(b"stale")
    (target / "histogram_456.db").write_bytes(b"stale")
    (target / "subdir").mkdir()
    (target / "subdir" / "x.db").write_bytes(b"stale")
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(target))

    conf.on_starting(SimpleNamespace())

    assert target.is_dir()
    assert list(target.iterdir()) == []


def test_on_starting_keeps_empty_existing_dir(conf, tmp_path, monkeypatch):
    target = tmp_path / "prometheus"
    target.mkdir()
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(target))
    conf.on_starting(SimpleNamespace())
    assert target.is_dir()
    assert list(target.iterdir()) == []


def test_on_starting_does_not_touch_siblings(conf, tmp_path, monkeypatch):
    # /dev/shm 也是 gunicorn worker_tmp_dir;只可清自己的子目錄。
    target = tmp_path / "prometheus"
    target.mkdir()
    sibling = tmp_path / "wgunicorn-heartbeat"
    sibling.write_bytes(b"keep")
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(target))
    conf.on_starting(SimpleNamespace())
    assert sibling.read_bytes() == b"keep"


@pytest.mark.parametrize("value", [None, ""])
def test_on_starting_noop_without_multiproc_dir(conf, tmp_path, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    else:
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", value)
    monkeypatch.chdir(tmp_path)
    conf.on_starting(SimpleNamespace())
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------- child_exit


def test_child_exit_marks_worker_dead(conf, tmp_path, monkeypatch):
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(conf, "mark_process_dead", lambda pid, *a, **kw: calls.append(pid))
    conf.child_exit(SimpleNamespace(), SimpleNamespace(pid=4321))
    assert calls == [4321]


def test_child_exit_removes_live_gauges_but_keeps_counters(conf, tmp_path, monkeypatch):
    # 真正呼叫 prometheus_client:live gauge 檔移除、counter 檔保留(counter 不歸零)。
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    (tmp_path / "gauge_livesum_4321.db").write_bytes(b"x")
    (tmp_path / "gauge_liveall_4321.db").write_bytes(b"x")
    (tmp_path / "counter_4321.db").write_bytes(b"x")
    (tmp_path / "gauge_livesum_9999.db").write_bytes(b"x")

    conf.child_exit(SimpleNamespace(), SimpleNamespace(pid=4321))

    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == ["counter_4321.db", "gauge_livesum_9999.db"]


@pytest.mark.parametrize("value", [None, ""])
def test_child_exit_noop_without_multiproc_dir(conf, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    else:
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", value)
    calls = []
    monkeypatch.setattr(conf, "mark_process_dead", lambda pid, *a, **kw: calls.append(pid))
    conf.child_exit(SimpleNamespace(), SimpleNamespace(pid=4321))
    assert calls == []
