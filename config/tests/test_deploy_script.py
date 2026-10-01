"""infra/scripts/deploy.sh(add-observability-stack design.md D4/D9 + 既有行為)。

deploy.sh 在本機執行、把遠端 script 經 `aws ssm send-command` 送到 EC2。測試以假的 `aws`
(擷取 send-command 的 parameters、回 Success)、`sleep`(不等 poll)放在 PATH 前面跑真正的
deploy.sh,取出遠端 script 後,把 `/opt/jiu-sync-backend` 換成暫存目錄,搭配假的 `docker`、
`getent`(macOS 沒有;給假的 passwd 行而不是讓 HOME 變空)實際執行。
"""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = REPO_ROOT / "infra" / "scripts" / "deploy.sh"
ALLOY_DIR = REPO_ROOT / "infra" / "alloy"
REMOTE_DIR = "/opt/jiu-sync-backend"

ALLOY_VARS = {
    "GRAFANA_CLOUD_PROM_URL": "https://prom.example.invalid/api/prom/push",
    "GRAFANA_CLOUD_PROM_USER": "111111",
    "GRAFANA_CLOUD_LOKI_URL": "https://logs.example.invalid/loki/api/v1/push",
    "GRAFANA_CLOUD_LOKI_USER": "222222",
    "GRAFANA_CLOUD_API_TOKEN": "glc_secret_api_token_value",
    "METRICS_TOKEN": "secret_metrics_token_value",
}
BASE_VARS = {
    "GHCR_USERNAME": "ghcr_user",
    "GHCR_TOKEN": "secret_ghcr_token_value",
    "DATABASE_URL": "postgres://u:secret_db_pw@pgbouncer:6432/d",
    "DATABASE_URL_DIRECT": "postgres://u:secret_db_pw@db:5432/d",
}


def _write_exe(path: Path, body: str):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture(scope="module")
def remote_script(tmp_path_factory):
    """跑真正的 deploy.sh(假 aws),回傳它送出的遠端 script。"""
    if shutil.which("jq") is None:
        if os.environ.get("CI"):
            pytest.fail("CI 環境缺少 jq,無法執行 deploy.sh")
        pytest.skip("jq 不存在,無法執行 deploy.sh")
    tmp = tmp_path_factory.mktemp("deploy_local")
    bindir = tmp / "bin"
    bindir.mkdir()
    params = tmp / "params.json"
    _write_exe(
        bindir / "aws",
        f"""#!/bin/bash
set -eu
case "$2" in
  send-command)
    while [ $# -gt 0 ]; do
      if [ "$1" = "--parameters" ]; then printf '%s' "$2" > "{params}"; fi
      shift
    done
    echo "fake-command-id"
    ;;
  get-command-invocation)
    echo '{{"Status":"Success","StandardOutputContent":"","StandardErrorContent":""}}'
    ;;
  *) echo "unexpected aws call: $*" >&2; exit 2 ;;
esac
""",
    )
    _write_exe(bindir / "sleep", "#!/bin/sh\nexit 0\n")
    result = subprocess.run(
        ["bash", str(DEPLOY_SH)],
        cwd=REPO_ROOT,
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    (cmd,) = json.loads(params.read_text())["commands"]
    return cmd


def _run_remote(remote_script: str, tmp_path: Path, env_vars: dict, restart_exit: int = 0):
    target = tmp_path / "opt"
    target.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (target / ".env").write_text("".join(f"{k}={v}\n" for k, v in env_vars.items()))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "docker_calls.log"
    _write_exe(
        bindir / "docker",
        f"""#!/bin/bash
echo "$*" >> "{calls}"
case "$*" in
  *" restart alloy") exit {restart_exit} ;;
esac
exit 0
""",
    )
    _write_exe(bindir / "getent", f'#!/bin/sh\necho "root:x:0:0:root:{home}:/bin/bash"\n')
    script = remote_script.replace(REMOTE_DIR, str(target))
    assert REMOTE_DIR not in script
    script_file = tmp_path / "remote.sh"
    script_file.write_text(script)
    result = subprocess.run(
        ["bash", str(script_file)],
        env={"PATH": f"{bindir}:/usr/bin:/bin:/usr/sbin:/sbin"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    docker_calls = calls.read_text().splitlines() if calls.exists() else []
    return result, target, docker_calls


ALL_VARS = {**BASE_VARS, **ALLOY_VARS}


def _secret_values(env_vars):
    return [v for k, v in env_vars.items() if k != "GHCR_USERNAME"]


def test_remote_writes_alloy_files_byte_identical(remote_script, tmp_path):
    result, target, _ = _run_remote(remote_script, tmp_path, ALL_VARS)
    assert result.returncode == 0, result.stdout + result.stderr
    for name in ("config.alloy", "redact.alloy"):
        assert (target / name).read_bytes() == (ALLOY_DIR / name).read_bytes(), name


def test_remote_compose_points_alloy_mounts_at_flat_files(remote_script, tmp_path):
    result, target, _ = _run_remote(remote_script, tmp_path, ALL_VARS)
    assert result.returncode == 0, result.stdout + result.stderr
    compose = (target / "docker-compose.prod.yml").read_text()
    assert "./config.alloy:/etc/alloy/config.alloy:ro" in compose
    assert "./redact.alloy:/etc/alloy/redact.alloy:ro" in compose
    assert "../alloy/" not in compose


def test_all_vars_present_no_warning_and_restart_after_up(remote_script, tmp_path):
    result, _, calls = _run_remote(remote_script, tmp_path, ALL_VARS)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARNING" not in result.stdout + result.stderr
    up = next(i for i, c in enumerate(calls) if c.endswith(" up -d"))
    restart = next(i for i, c in enumerate(calls) if c.endswith(" restart alloy"))
    assert restart > up
    assert calls[restart].startswith("compose -f ")


@pytest.mark.parametrize("missing", sorted(ALLOY_VARS))
def test_missing_alloy_var_warns_and_continues(remote_script, tmp_path, missing):
    env_vars = {k: v for k, v in ALL_VARS.items() if k != missing}
    result, _, calls = _run_remote(remote_script, tmp_path, env_vars)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "WARNING" in out and missing in out
    assert any(c.endswith(" up -d") for c in calls)
    assert any(c.endswith(" restart alloy") for c in calls)
    assert "Deploy finished successfully" in out


def test_empty_alloy_var_warns_and_continues(remote_script, tmp_path):
    env_vars = {**ALL_VARS, "GRAFANA_CLOUD_API_TOKEN": ""}
    result, _, calls = _run_remote(remote_script, tmp_path, env_vars)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "WARNING" in out and "GRAFANA_CLOUD_API_TOKEN" in out
    assert any(c.endswith(" up -d") for c in calls)


def test_all_alloy_vars_missing_lists_each_and_continues(remote_script, tmp_path):
    result, _, calls = _run_remote(remote_script, tmp_path, BASE_VARS)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    for key in ALLOY_VARS:
        assert key in out
    assert any(c.endswith(" up -d") for c in calls)


def test_present_var_names_not_reported_missing(remote_script, tmp_path):
    env_vars = {k: v for k, v in ALL_VARS.items() if k != "METRICS_TOKEN"}
    result, _, _ = _run_remote(remote_script, tmp_path, env_vars)
    warning_lines = [
        line for line in (result.stdout + result.stderr).splitlines() if "WARNING" in line
    ]
    joined = "\n".join(warning_lines)
    assert "METRICS_TOKEN" in joined
    assert "GRAFANA_CLOUD_PROM_URL" not in joined


def test_alloy_var_check_never_prints_values(remote_script, tmp_path):
    env_vars = {k: v for k, v in ALL_VARS.items() if k != "GRAFANA_CLOUD_PROM_USER"}
    result, _, _ = _run_remote(remote_script, tmp_path, env_vars)
    out = result.stdout + result.stderr
    for value in _secret_values(env_vars):
        assert value not in out


def test_restart_failure_only_warns(remote_script, tmp_path):
    result, _, calls = _run_remote(remote_script, tmp_path, ALL_VARS, restart_exit=1)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "WARNING" in out and "alloy" in out
    assert "Deploy finished successfully" in out
    # migrate / collectstatic 仍照常執行
    assert any("manage.py migrate" in c for c in calls)
    assert any("collectstatic" in c for c in calls)


def test_missing_database_url_direct_still_aborts_before_pull(remote_script, tmp_path):
    env_vars = {k: v for k, v in ALL_VARS.items() if k != "DATABASE_URL_DIRECT"}
    result, _, calls = _run_remote(remote_script, tmp_path, env_vars)
    assert result.returncode != 0
    assert "DATABASE_URL_DIRECT" in result.stderr
    assert not any(" pull" in c or " up -d" in c or "restart" in c for c in calls)


def test_missing_env_file_aborts_before_docker(remote_script, tmp_path):
    # 目錄存在但沒有 .env
    script = remote_script.replace(REMOTE_DIR, str(tmp_path / "nope"))
    (tmp_path / "nope").mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    _write_exe(bindir / "docker", f'#!/bin/bash\necho "$*" >> "{calls}"\n')
    _write_exe(bindir / "getent", f'#!/bin/sh\necho "root:x:0:0:root:{tmp_path}:/bin/bash"\n')
    script_file = tmp_path / "remote.sh"
    script_file.write_text(script)
    result = subprocess.run(
        ["bash", str(script_file)],
        env={"PATH": f"{bindir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert not calls.exists()
    assert not (tmp_path / "nope" / "config.alloy").exists()


def test_migrate_uses_direct_url(remote_script, tmp_path):
    result, _, calls = _run_remote(remote_script, tmp_path, ALL_VARS)
    assert result.returncode == 0, result.stdout + result.stderr
    (migrate,) = [c for c in calls if "manage.py migrate" in c]
    assert f"DATABASE_URL={BASE_VARS['DATABASE_URL_DIRECT']}" in migrate


@pytest.mark.parametrize("missing", ["DATABASE_URL_DIRECT", "GHCR_TOKEN"])
def test_aborted_deploy_does_not_write_alloy_files(remote_script, tmp_path, missing):
    # 中止的部署不可留下新的 alloy 設定(否則之後 restart/重開機會在未部署的情況下載入新設定)
    env_vars = {k: v for k, v in ALL_VARS.items() if k != missing}
    result, target, _ = _run_remote(remote_script, tmp_path, env_vars)
    assert result.returncode != 0
    assert not (target / "config.alloy").exists()
    assert not (target / "redact.alloy").exists()


def test_successful_deploy_never_prints_secret_values(remote_script, tmp_path):
    result, _, _ = _run_remote(remote_script, tmp_path, ALL_VARS)
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout + result.stderr
    for value in _secret_values(ALL_VARS):
        assert value not in out
