"""正式 compose 的 alloy service(add-observability-stack design.md D4/D5/D8/D9)。

沿用 test_prod_compose.py 的做法:以 `docker compose config --format json` 解析(compose 自己的
正規化結果),compose 檔複製到暫存目錄的相同相對位置、旁邊放假 .env,不讀 repo 真正的 .env。

另外驗證 deploy.sh 的平面佈局改寫:EC2 上 compose 檔與 config.alloy / redact.alloy 都直接放在
/opt/jiu-sync-backend/(D4),bind mount 來源要能在 repo 內(infra/docker/)與平面佈局兩邊都成立。
"""

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROD_COMPOSE = REPO_ROOT / "infra" / "docker" / "docker-compose.prod.yml"
DEPLOY_SH = REPO_ROOT / "infra" / "scripts" / "deploy.sh"

ALLOY_ENV_VARS = {
    "GRAFANA_CLOUD_PROM_URL": "https://prom.example.invalid/api/prom/push",
    "GRAFANA_CLOUD_PROM_USER": "111111",
    "GRAFANA_CLOUD_LOKI_URL": "https://logs.example.invalid/loki/api/v1/push",
    "GRAFANA_CLOUD_LOKI_USER": "222222",
    "GRAFANA_CLOUD_API_TOKEN": "glc_fake_api_token_value",
    "METRICS_TOKEN": "fake_metrics_token_value",
}

# 其他機密:絕不可出現在 alloy service 的任何地方(D5)
OTHER_SECRETS = {
    "SECRET_KEY": "fake_django_secret_key_value",
    "POSTGRES_PASSWORD": "fake_password_value",
    "DATABASE_URL": "postgres://fake_user:fake_password_value@pgbouncer:6432/fake_db",
    "DATABASE_URL_DIRECT": "postgres://fake_user:fake_password_value@db:5432/fake_db",
    "GHCR_TOKEN": "fake_ghcr_token_value",
    "EMAIL_HOST_PASSWORD": "fake_email_password_value",
}

FAKE_ENV = (
    "DJANGO_SETTINGS_MODULE=config.settings.dev\n"
    "POSTGRES_DB=fake_db\n"
    "POSTGRES_USER=fake_user\n"
    "CELERY_BROKER_URL=redis://redis:6379/0\n"
    + "".join(f"{k}={v}\n" for k, v in {**OTHER_SECRETS, **ALLOY_ENV_VARS}.items())
)

# D8:所有 host bind mount(唯讀)。container 內路徑 -> host 路徑
EXPECTED_HOST_MOUNTS = {
    "/var/run/docker.sock": "/var/run/docker.sock",
    "/run/containerd/containerd.sock": "/run/containerd/containerd.sock",
    "/sys": "/sys",
    "/rootfs": "/",
    "/host/proc": "/proc",
    "/var/lib/docker": "/var/lib/docker",
    "/dev/disk": "/dev/disk",
    "/var/log/nginx": "/var/log/nginx",
}
# D4:repo 內的設定檔(container 內路徑 -> 檔名)
ALLOY_CONFIG_FILES = {
    "/etc/alloy/config.alloy": "config.alloy",
    "/etc/alloy/redact.alloy": "redact.alloy",
}


def _docker_or_skip():
    docker = shutil.which("docker")
    if docker is None:
        if os.environ.get("CI"):
            pytest.fail("CI 環境缺少 docker CLI,無法驗證正式 compose 的 alloy 定義")
        pytest.skip("docker CLI 不存在,無法以 docker compose config 解析正式 compose")
    return docker


def _compose_config(docker, compose_file: Path, env_file: Path) -> dict:
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
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def repo_layout(tmp_path_factory):
    """repo 內佈局:compose 在 <root>/infra/docker/,設定檔在 <root>/infra/alloy/。"""
    docker = _docker_or_skip()
    root = tmp_path_factory.mktemp("alloy_repo_layout")
    compose_dir = root / "infra" / "docker"
    compose_dir.mkdir(parents=True)
    compose_file = compose_dir / PROD_COMPOSE.name
    shutil.copy(PROD_COMPOSE, compose_file)
    env_file = root / ".env"
    env_file.write_text(FAKE_ENV)
    return root, _compose_config(docker, compose_file, env_file)


@pytest.fixture(scope="module")
def config(repo_layout):
    return repo_layout[1]


@pytest.fixture(scope="module")
def alloy(config):
    assert "alloy" in config["services"], "正式 compose 缺少 alloy service"
    return config["services"]["alloy"]


def _bind_mounts(service):
    return [v for v in service.get("volumes", []) if v["type"] == "bind"]


def test_alloy_image_pinned_exactly(alloy):
    assert alloy["image"] == "grafana/alloy:v1.20.1"


def test_alloy_mem_limit_200m(alloy):
    assert int(alloy["mem_limit"]) == 200 * 1024 * 1024


def test_alloy_restarts_unless_stopped(alloy):
    assert alloy["restart"] == "unless-stopped"


def test_alloy_publishes_no_ports(alloy):
    assert not alloy.get("ports")
    assert not alloy.get("expose")


def test_alloy_not_privileged_no_caps_no_host_pid(alloy):
    assert not alloy.get("privileged")
    assert not alloy.get("cap_add")
    assert "pid" not in alloy
    assert alloy.get("network_mode") in (None, "")
    assert not alloy.get("devices")
    assert not alloy.get("security_opt")


def test_alloy_not_in_any_depends_on(config):
    for name, svc in config["services"].items():
        assert "alloy" not in (svc.get("depends_on") or {}), f"{name} depends_on alloy"


def test_alloy_has_no_depends_on_itself(alloy):
    # 故障隔離(D9):alloy 不拉起、也不綁住其他 service
    assert not alloy.get("depends_on")


def test_alloy_has_no_env_file(alloy):
    assert not alloy.get("env_file")


def test_alloy_environment_is_exactly_six_vars_plus_gomemlimit(alloy):
    assert set(alloy["environment"]) == set(ALLOY_ENV_VARS) | {"GOMEMLIMIT"}


def test_alloy_gomemlimit_150mib(alloy):
    assert alloy["environment"]["GOMEMLIMIT"] == "150MiB"


def test_alloy_env_values_flow_from_dotenv(alloy):
    for key, value in ALLOY_ENV_VARS.items():
        assert alloy["environment"][key] == value, key


def test_alloy_env_uses_interpolation_not_literals():
    # 來源檔必須寫成 ${VAR},值只存在 .env(不可把值寫死在 compose)
    text = PROD_COMPOSE.read_text()
    for key in ALLOY_ENV_VARS:
        assert re.search(rf"^\s+{key}:\s+\$\{{{key}\}}\s*$", text, re.M), key


def test_other_secrets_absent_from_alloy_service(alloy):
    dumped = json.dumps(alloy)
    for key, value in OTHER_SECRETS.items():
        assert key not in dumped, key
        assert value not in dumped, key


def test_alloy_host_mounts_exact_and_read_only(alloy, repo_layout):
    root = repo_layout[0]
    host = {
        v["target"]: v["source"]
        for v in _bind_mounts(alloy)
        if not v["source"].startswith(str(root))
    }
    assert host == EXPECTED_HOST_MOUNTS


def test_every_bind_mount_is_read_only(alloy):
    binds = _bind_mounts(alloy)
    assert binds
    for v in binds:
        assert v.get("read_only") is True, v["target"]


def test_alloy_config_files_mounted_from_repo_alloy_dir(alloy, repo_layout):
    root = repo_layout[0]
    mounts = {v["target"]: v["source"] for v in _bind_mounts(alloy)}
    for target, name in ALLOY_CONFIG_FILES.items():
        assert mounts.get(target) == str(root / "infra" / "alloy" / name), target


def test_no_other_bind_mounts(alloy):
    targets = {v["target"] for v in _bind_mounts(alloy)}
    assert targets == set(EXPECTED_HOST_MOUNTS) | set(ALLOY_CONFIG_FILES)


def test_alloy_data_named_volume_rw(alloy, config):
    vols = [v for v in alloy.get("volumes", []) if v["type"] == "volume"]
    assert len(vols) == 1
    (vol,) = vols
    assert vol["source"] == "alloy_data"
    assert vol["target"] == "/var/lib/alloy/data"
    assert not vol.get("read_only")
    assert "alloy_data" in config.get("volumes", {})


def test_alloy_command_uses_storage_path_and_config(alloy):
    cmd = alloy["command"]
    assert cmd[0] == "run"
    assert "--storage.path=/var/lib/alloy/data" in cmd
    assert "/etc/alloy/config.alloy" in cmd
    # UI 只聽 container 內 loopback(預設值);不可改成 0.0.0.0
    assert not any(c.startswith("--server.http.listen-addr") for c in cmd)


def test_alloy_runs_as_root_default(alloy):
    # D8:以 root 執行(讀 root:adm 640 的 nginx log);image 預設即 root,不另設 user
    assert alloy.get("user") in (None, "", "0", "root")


def test_config_files_exist_in_repo():
    for name in ALLOY_CONFIG_FILES.values():
        assert (REPO_ROOT / "infra" / "alloy" / name).is_file(), name


# --- deploy.sh 的平面佈局改寫(D4)---------------------------------------


def _deploy_rewrite_sed() -> list[str]:
    """取出 deploy.sh 對 compose 檔所用的 sed 運算式,讓測試跑與正式完全相同的改寫。"""
    text = DEPLOY_SH.read_text()
    m = re.search(r"^COMPOSE_B64=\$\(sed (.+?) \"\$COMPOSE_FILE_LOCAL\" \|", text, re.M)
    assert m, "deploy.sh 找不到 COMPOSE_B64 的 sed 改寫"
    return shlex.split(m.group(1))


def test_flat_remote_layout_resolves_env_and_config_files(tmp_path):
    docker = _docker_or_skip()
    flat = tmp_path / "opt_jiu_sync_backend"
    flat.mkdir()
    rewritten = subprocess.run(
        ["sed", *_deploy_rewrite_sed(), str(PROD_COMPOSE)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    compose_file = flat / "docker-compose.prod.yml"
    compose_file.write_text(rewritten)
    (flat / ".env").write_text(FAKE_ENV)
    for name in ALLOY_CONFIG_FILES.values():
        (flat / name).write_text("// placeholder\n")

    # 不加 --env-file:正式環境靠 compose 自動讀 project 目錄(= compose 檔所在目錄)的 .env
    result = subprocess.run(
        [docker, "compose", "-f", str(compose_file), "config", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    alloy = services["alloy"]
    mounts = {v["target"]: v for v in _bind_mounts(alloy)}
    for target, name in ALLOY_CONFIG_FILES.items():
        assert mounts[target]["source"] == str(flat / name)
        assert mounts[target]["read_only"] is True
    for key, value in ALLOY_ENV_VARS.items():
        assert alloy["environment"][key] == value
    # 既有的 env_file 改寫仍成立
    assert services["app"]["environment"]["DATABASE_URL"] == OTHER_SECRETS["DATABASE_URL"]


def test_rewrite_leaves_header_prose_and_host_paths_untouched():
    rewritten = subprocess.run(
        ["sed", *_deploy_rewrite_sed(), str(PROD_COMPOSE)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    original = PROD_COMPOSE.read_text()
    header = original.split("services:", 1)[0]
    assert rewritten.startswith(header)
    assert "../alloy/" not in rewritten.split("services:", 1)[1]
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in rewritten
