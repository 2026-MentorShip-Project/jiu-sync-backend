"""正式 compose 的 celery worker 定義(add-pgbouncer-and-celery-worker design.md D5)。

以 `docker compose config --format json` 解析 infra/docker/docker-compose.prod.yml
(compose 自己的正規化結果,而非自寫 YAML 解析),守住 worker 的接線:同一 image、
concurrency=1、明確 prod settings、經 PgBouncer 與 redis、自動重啟、不綁 host port。

compose 檔以 `../../.env` 讀 env_file;為避免讀到(甚至在斷言失敗時印出)repo 根目錄
真正的 .env,測試把 compose 檔複製到暫存目錄的相同相對位置,旁邊放假 .env。
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROD_COMPOSE = REPO_ROOT / "infra" / "docker" / "docker-compose.prod.yml"

# 假 .env 故意把 DJANGO_SETTINGS_MODULE 設成 dev,驗證 worker 會明確覆寫成 prod。
FAKE_ENV = """\
DJANGO_SETTINGS_MODULE=config.settings.dev
POSTGRES_DB=fake_db
POSTGRES_USER=fake_user
POSTGRES_PASSWORD=fake_password
DATABASE_URL=postgres://fake_user:fake_password@pgbouncer:6432/fake_db
DATABASE_URL_DIRECT=postgres://fake_user:fake_password@db:5432/fake_db
CELERY_BROKER_URL=redis://redis:6379/0
"""


@pytest.fixture(scope="module")
def prod_services(tmp_path_factory):
    docker = shutil.which("docker")
    if docker is None:
        # CI(ubuntu-latest)一定有 docker;在 CI 缺 docker 代表環境變了,不可默默 skip。
        if os.environ.get("CI"):
            pytest.fail("CI 環境缺少 docker CLI,無法驗證正式 compose 的 worker 定義")
        pytest.skip("docker CLI 不存在,無法以 docker compose config 解析正式 compose")
    root = tmp_path_factory.mktemp("prod_compose")
    compose_dir = root / "infra" / "docker"
    compose_dir.mkdir(parents=True)
    compose_file = compose_dir / PROD_COMPOSE.name
    shutil.copy(PROD_COMPOSE, compose_file)
    env_file = root / ".env"
    env_file.write_text(FAKE_ENV)

    result = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            str(compose_file),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["services"]


@pytest.fixture(scope="module")
def worker(prod_services):
    assert "worker" in prod_services, "正式 compose 缺少 worker service"
    return prod_services["worker"]


def test_worker_uses_same_image_as_app(prod_services, worker):
    assert worker["image"] == prod_services["app"]["image"]


def test_worker_runs_celery_with_concurrency_one(worker):
    assert worker["command"] == [
        "celery",
        "-A",
        "config",
        "worker",
        "-l",
        "info",
        "--concurrency=1",
    ]


def test_worker_explicitly_uses_prod_settings_even_if_env_file_says_otherwise(worker):
    assert worker["environment"]["DJANGO_SETTINGS_MODULE"] == "config.settings.prod"


def test_worker_reads_same_env_file_as_app(prod_services, worker):
    # 同一份 .env → 同一個 broker URL 與 DATABASE_URL(經 PgBouncer)
    app_env = prod_services["app"]["environment"]
    assert worker["environment"]["CELERY_BROKER_URL"] == app_env["CELERY_BROKER_URL"]
    assert worker["environment"]["DATABASE_URL"] == app_env["DATABASE_URL"]


def test_worker_depends_on_pgbouncer_and_redis_not_db_directly(worker):
    assert set(worker["depends_on"]) == {"pgbouncer", "redis"}


def test_worker_restarts_unless_stopped(worker):
    assert worker["restart"] == "unless-stopped"


def test_worker_binds_no_host_port(worker):
    assert not worker.get("ports")


def test_header_requires_broker_url_pointing_at_redis_service():
    header = PROD_COMPOSE.read_text().split("services:", 1)[0]
    assert "CELERY_BROKER_URL=redis://redis:6379/0" in header
    # 需明講 worker 也吃這個值、且不可用 localhost(container 內不可達)
    assert "worker" in header
    assert "localhost:6381" in header


def test_app_sets_prometheus_multiproc_dir_on_tmpfs(prod_services):
    # add-observability-stack design.md D2:放在 container 自己的 /dev/shm,重啟即清空。
    app_env = prod_services["app"]["environment"]
    assert app_env["PROMETHEUS_MULTIPROC_DIR"] == "/dev/shm/prometheus"


def test_worker_does_not_set_prometheus_multiproc_dir(worker):
    # D2:只有 app 設;celery worker 不設(不寫 multiprocess 檔)。
    assert "PROMETHEUS_MULTIPROC_DIR" not in worker["environment"]


def test_app_explicitly_uses_prod_settings_same_as_worker(prod_services, worker):
    # design.md D6:假 .env 設 dev,app 仍須解析為 prod,且與 worker 相同(不靠 wsgi.py setdefault)
    app_env = prod_services["app"]["environment"]
    assert app_env["DJANGO_SETTINGS_MODULE"] == "config.settings.prod"
    assert app_env["DJANGO_SETTINGS_MODULE"] == worker["environment"]["DJANGO_SETTINGS_MODULE"]
