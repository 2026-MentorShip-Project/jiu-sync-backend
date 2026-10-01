"""infra/alloy/config.alloy 與 redact.alloy(add-observability-stack design.md D3/D6/D7)。

全部用真正的 `grafana/alloy:v1.20.1`(與正式 compose 同一個 image)驗證:
- `alloy fmt`:兩個檔案都已是 fmt 的正規格式。
- 載入:以 dummy 環境變數實際跑完整 config.alloy,等 `/-/ready`,再從 Alloy 自己的
  component API(`/api/v0/web/components`)讀出解析後的參數與資料流向來斷言(不是 grep 原始碼)。
  本機掛載與正式不同(見 `_run_full_config`),只影響資料來源,不影響設定載入。
- 遮蔽(D7):harness 以 `import.file` 匯入正式的 redact.alloy(與 config.alloy 匯入的是同一份檔案,
  不是複製一份規則),`loki.source.file` 讀樣本 log → 正式遮蔽 → `loki.echo`,比對輸出。

docker 不存在時本機 skip、CI 直接 fail(同 test_prod_compose.py)。
"""

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOY_DIR = REPO_ROOT / "infra" / "alloy"
CONFIG = ALLOY_DIR / "config.alloy"
REDACT = ALLOY_DIR / "redact.alloy"
IMAGE = "grafana/alloy:v1.20.1"

DUMMY_ENV = {
    "GRAFANA_CLOUD_PROM_URL": "http://127.0.0.1:9/api/prom/push",
    "GRAFANA_CLOUD_PROM_USER": "111111",
    "GRAFANA_CLOUD_LOKI_URL": "http://127.0.0.1:9/loki/api/v1/push",
    "GRAFANA_CLOUD_LOKI_USER": "222222",
    "GRAFANA_CLOUD_API_TOKEN": "dummy_api_token",
    "METRICS_TOKEN": "dummy_metrics_token",
    "GOMEMLIMIT": "150MiB",
}

CADVISOR_ALLOWLIST = {
    "container_cpu_usage_seconds_total",
    "container_memory_working_set_bytes",
    "container_memory_rss",
    "container_network_receive_bytes_total",
    "container_network_transmit_bytes_total",
    "container_start_time_seconds",
    "container_last_seen",
}


def _bind(src, dst):
    """--mount 而不是 -v:來源不存在時 docker 直接報錯,不會在 repo 內自動建立空目錄。"""
    return ["--mount", f"type=bind,src={src},dst={dst},readonly"]


def _docker_or_skip():
    docker = shutil.which("docker")
    if docker is None:
        if os.environ.get("CI"):
            pytest.fail("CI 環境缺少 docker CLI,無法以真正的 Alloy 驗證設定")
        pytest.skip("docker CLI 不存在,無法以真正的 Alloy 驗證設定")
    return docker


# --- Alloy component API 參數轉成 Python 值 --------------------------------


def _value(v):
    t, val = v["type"], v.get("value")
    if t == "array":
        return [_value(x) for x in val]
    if t == "object":
        return {item["key"]: _value(item["value"]) for item in val}
    if t == "capsule":
        return val  # secret 一律顯示為 "(secret)"
    return val


def _body(items):
    out: dict = {}
    for item in items:
        if item["type"] == "attr":
            out[item["name"]] = _value(item["value"])
        else:  # block,可重複
            out.setdefault(item["name"], []).append(_body(item.get("body", [])))
    return out


# --- 完整設定載入 ---------------------------------------------------------


def _http_get(url, timeout=2):
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - 本機 loopback
        return resp.status, resp.read().decode()


def _run_full_config(docker, tmp: Path, name: str):
    """以正式 config.alloy + redact.alloy 跑 Alloy。

    與正式 compose 的差異(只為了在任何 docker 主機上都能跑,含 macOS Docker Desktop):
    - /host/proc、/rootfs、/var/log/nginx 改掛暫存目錄(nginx 放空的 access/error.log);
    - 不掛 containerd.sock 與 host /sys(Docker Desktop 主機上不存在/不同),cadvisor 在本機拿不到
      container metrics 屬已知,不影響設定載入;
    - publish UI port 到 127.0.0.1 隨機 port、listen 0.0.0.0,只為了讓測試讀 component API。
    """
    for d in ("proc", "rootfs", "nginx"):
        (tmp / d).mkdir()
    (tmp / "nginx" / "access.log").write_text("")
    (tmp / "nginx" / "error.log").write_text("")
    cmd = [docker, "run", "-d", "--name", name, "-p", "127.0.0.1::12345"]
    for k, v in DUMMY_ENV.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [
        *_bind(CONFIG, "/etc/alloy/config.alloy"),
        *_bind(REDACT, "/etc/alloy/redact.alloy"),
        *_bind("/var/run/docker.sock", "/var/run/docker.sock"),
        *_bind(tmp / "proc", "/host/proc"),
        *_bind(tmp / "rootfs", "/rootfs"),
        *_bind(tmp / "nginx", "/var/log/nginx"),
        IMAGE,
        "run",
        "--server.http.listen-addr=0.0.0.0:12345",
        "--storage.path=/var/lib/alloy/data",
        "/etc/alloy/config.alloy",
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)


@pytest.fixture(scope="module")
def full_config(tmp_path_factory):
    docker = _docker_or_skip()
    tmp = tmp_path_factory.mktemp("alloy_full")
    name = f"alloy-config-test-{uuid.uuid4().hex[:8]}"
    try:
        # 放在 try 內:docker run -d 可能已建立 container 但啟動失敗,仍需清掉
        _run_full_config(docker, tmp, name)
        port = (
            subprocess.run(
                [docker, "port", name, "12345"],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            .stdout.splitlines()[0]
            .strip()
        )
        base = f"http://{port}"
        deadline = time.time() + 60
        ready = False
        while time.time() < deadline:
            try:
                status, _ = _http_get(f"{base}/-/ready")
                if status == 200:
                    ready = True
                    break
            except Exception:  # noqa: BLE001 - 啟動中連線失敗是預期
                pass
            time.sleep(1)
        logs = subprocess.run(
            [docker, "logs", name], capture_output=True, text=True, timeout=30
        ).stderr
        assert ready, f"Alloy 未在 60 秒內 ready:\n{logs[-4000:]}"
        _, body = _http_get(f"{base}/api/v0/web/components", timeout=10)
        components = {}
        for c in json.loads(body):
            _, detail = _http_get(f"{base}/api/v0/web/components/{c['localID']}", timeout=10)
            components[c["localID"]] = json.loads(detail)
        running = subprocess.run(
            [docker, "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        yield {"components": components, "logs": logs, "running": running}
    finally:
        subprocess.run([docker, "rm", "-f", name], capture_output=True, timeout=60)


@pytest.fixture(scope="module")
def comps(full_config):
    return full_config["components"]


def _args(comps, local_id):
    assert local_id in comps, f"缺少 component {local_id};現有:{sorted(comps)}"
    return _body(comps[local_id]["arguments"])


def _refs(comps, local_id):
    return set(comps[local_id]["referencesTo"])


def _ids_of(comps, name):
    return {k for k, c in comps.items() if c["name"] == name}


# -- fmt --


@pytest.mark.parametrize("path", [CONFIG, REDACT], ids=lambda p: p.name)
def test_alloy_fmt_is_canonical(path):
    docker = _docker_or_skip()
    result = subprocess.run(
        [docker, "run", "--rm", *_bind(path, "/tmp/check.alloy"), IMAGE, "fmt", "/tmp/check.alloy"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == path.read_text(), "檔案不是 alloy fmt 的正規格式"


# -- 載入 --


def test_full_config_loads_and_stays_running(full_config):
    assert full_config["running"] == "true", full_config["logs"][-4000:]


def test_startup_logs_have_no_diskstats_udev_error(full_config):
    # D6:diskstats 不收,啟動 log 不應出現 diskstats / /run/udev 的錯誤
    bad = [
        line
        for line in full_config["logs"].splitlines()
        if "level=error" in line and ("diskstats" in line or "/run/udev" in line)
    ]
    assert not bad, "\n".join(bad)


def test_no_component_unhealthy(comps):
    bad = {k: c["health"] for k, c in comps.items() if c["health"]["state"] not in ("healthy",)}
    assert not bad


# -- D3/D6 metrics --


def test_app_scrape_uses_bearer_from_env_every_30s(comps):
    a = _args(comps, "prometheus.scrape.app")
    assert a["targets"] == [{"__address__": "app:8000"}]
    assert a.get("metrics_path", "/metrics") == "/metrics"
    assert a.get("scheme", "http") == "http"
    assert a["scrape_interval"] == "30s"
    (auth,) = a["authorization"]
    assert auth["type"] == "Bearer"
    assert auth["credentials"] == "(secret)"
    assert _refs(comps, "prometheus.scrape.app") == {"prometheus.remote_write.grafana_cloud"}


def test_metrics_token_read_via_sys_env():
    text = CONFIG.read_text()
    assert re.search(r'credentials\s*=\s*sys\.env\("METRICS_TOKEN"\)', text)


def test_cadvisor_params_per_d6(comps):
    a = _args(comps, "prometheus.exporter.cadvisor.containers")
    assert a["docker_only"] is True
    assert sorted(a["enabled_metrics"]) == ["cpu", "memory", "network"]
    # D6 的 housekeeping_interval = "30s" 在 Alloy v1.20.1 不是合法參數(設定整份載入失敗),
    # 無法實作;待 opsx:update 修正 D6(見 tasks.md 3.1 結果、incident log)。


def test_cadvisor_scraped_every_30s_through_relabel(comps):
    a = _args(comps, "prometheus.scrape.cadvisor")
    assert a["scrape_interval"] == "30s"
    assert _refs(comps, "prometheus.scrape.cadvisor") == {
        "prometheus.exporter.cadvisor.containers",
        "prometheus.relabel.cadvisor",
    }
    assert _refs(comps, "prometheus.relabel.cadvisor") == {"prometheus.remote_write.grafana_cloud"}


def _rules(comps, local_id):
    return _args(comps, local_id)["rule"]


def _prom_full(regex, value):
    # Prometheus relabel 的 regex 是完整比對(自動加錨點)
    return re.fullmatch(regex, value) is not None


def test_cadvisor_relabel_keeps_only_allowlist(comps):
    rules = _rules(comps, "prometheus.relabel.cadvisor")
    keeps = [r for r in rules if r.get("action") == "keep"]
    assert len(keeps) == 1
    (keep,) = keeps
    assert keep["source_labels"] == ["__name__"]
    for name in CADVISOR_ALLOWLIST:
        assert _prom_full(keep["regex"], name), name
    for name in (
        "container_memory_cache",
        "container_fs_usage_bytes",
        "container_cpu_system_seconds_total",
        "container_network_receive_packets_total",
        "container_memory_rss_extra",
        "xcontainer_memory_rss",
        "node_memory_MemAvailable_bytes",
    ):
        assert not _prom_full(keep["regex"], name), name


def test_cadvisor_relabel_drops_root_cgroup(comps):
    rules = _rules(comps, "prometheus.relabel.cadvisor")
    drops = [r for r in rules if r.get("action") == "drop"]
    assert any(
        r["source_labels"] == ["name"]
        and _prom_full(r["regex"], "")
        and not _prom_full(r["regex"], "app")
        for r in drops
    )


def test_cadvisor_relabel_drops_high_cardinality_labels(comps):
    rules = _rules(comps, "prometheus.relabel.cadvisor")
    labeldrops = [r["regex"] for r in rules if r.get("action") == "labeldrop"]
    assert labeldrops

    def dropped(label):
        return any(_prom_full(rx, label) for rx in labeldrops)

    for label in ("id", "image", "container_label_com_docker_compose_project"):
        assert dropped(label), label
    for label in ("name", "__name__", "interface", "cpu", "job", "instance"):
        assert not dropped(label), label
    assert not any(r.get("action") == "labelkeep" for r in rules)


def test_unix_exporter_paths_and_collectors(comps):
    a = _args(comps, "prometheus.exporter.unix.host")
    assert a["procfs_path"] == "/host/proc"
    # component API 只列出與預設值不同的參數;sysfs_path 預設即 /sys,另以原始碼確認有明寫
    assert a.get("sysfs_path", "/sys") == "/sys"
    assert re.search(r'^\s*sysfs_path\s*=\s*"/sys"\s*$', CONFIG.read_text(), re.M)
    assert a["rootfs_path"] == "/rootfs"
    # D6:不收 diskstats(未掛 /run/udev 時每次啟動都會記一行錯誤 log)
    assert sorted(a["set_collectors"]) == sorted(
        ["cpu", "meminfo", "filesystem", "netdev", "loadavg"]
    )
    s = _args(comps, "prometheus.scrape.host")
    assert s["scrape_interval"] == "30s"
    assert _refs(comps, "prometheus.scrape.host") == {
        "prometheus.exporter.unix.host",
        "prometheus.remote_write.grafana_cloud",
    }


def test_single_remote_write_with_env_url_and_basic_auth(comps):
    assert _ids_of(comps, "prometheus.remote_write") == {"prometheus.remote_write.grafana_cloud"}
    a = _args(comps, "prometheus.remote_write.grafana_cloud")
    (endpoint,) = a["endpoint"]
    assert endpoint["url"] == DUMMY_ENV["GRAFANA_CLOUD_PROM_URL"]  # 原樣使用,不拼接路徑
    (basic,) = endpoint["basic_auth"]
    assert basic["username"] == DUMMY_ENV["GRAFANA_CLOUD_PROM_USER"]
    assert basic["password"] == "(secret)"


# -- D7 logs --


def test_docker_logs_labelled_container_without_leading_slash(comps):
    rules = _rules(comps, "discovery.relabel.docker_logs")
    (rule,) = [r for r in rules if r.get("target_label") == "container"]
    assert rule["source_labels"] == ["__meta_docker_container_name"]
    m = re.fullmatch(rule["regex"], "/jiu-sync-backend-app-1")
    assert m
    replacement = rule.get("replacement", "$1")
    assert re.sub(r"\$\{?(\d+)\}?", lambda g: m.group(int(g.group(1))), replacement) == (
        "jiu-sync-backend-app-1"
    )
    d = _args(comps, "discovery.docker.containers")
    assert d["host"] == "unix:///var/run/docker.sock"
    src = _args(comps, "loki.source.docker.containers")
    assert src["host"] == "unix:///var/run/docker.sock"
    assert _refs(comps, "loki.source.docker.containers") == {
        "discovery.docker.containers",
        "discovery.relabel.docker_logs",
        "redact.secrets.logs",
    }


def test_nginx_file_targets(comps):
    a = _args(comps, "loki.source.file.nginx")
    targets = sorted(a["targets"], key=lambda t: t["__path__"])
    assert targets == [
        {"__path__": "/var/log/nginx/access.log", "source": "nginx", "log_type": "access"},
        {"__path__": "/var/log/nginx/error.log", "source": "nginx", "log_type": "error"},
    ]
    assert _refs(comps, "loki.source.file.nginx") == {"redact.secrets.logs"}


def test_every_log_source_goes_through_redaction_only(comps):
    sources = {k for k, c in comps.items() if c["name"].startswith("loki.source.")}
    assert sources == {"loki.source.docker.containers", "loki.source.file.nginx"}
    (write,) = _ids_of(comps, "loki.write")
    assert set(comps[write]["referencedBy"]) == {"redact.secrets.logs"}
    assert _refs(comps, "redact.secrets.logs") == {write}


def test_single_loki_write_with_env_url_and_basic_auth(comps):
    assert _ids_of(comps, "loki.write") == {"loki.write.grafana_cloud"}
    a = _args(comps, "loki.write.grafana_cloud")
    (endpoint,) = a["endpoint"]
    assert endpoint["url"] == DUMMY_ENV["GRAFANA_CLOUD_LOKI_URL"]
    (basic,) = endpoint["basic_auth"]
    assert basic["username"] == DUMMY_ENV["GRAFANA_CLOUD_LOKI_USER"]
    assert basic["password"] == "(secret)"


def test_config_imports_redact_module_from_etc_alloy(comps):
    # import.file 不會出現在 component API;redact.secrets.logs 存在即代表匯入成功,
    # 路徑以原始碼確認(載入測試把 redact.alloy 掛在同一路徑,與 compose 相同)
    assert comps["redact.secrets.logs"]["name"] == "redact.secrets"
    assert re.search(
        r'import\.file "redact" \{\s*filename = "/etc/alloy/redact\.alloy"\s*\}', CONFIG.read_text()
    )


# -- D7 遮蔽(真正的 Alloy + 正式 redact.alloy)--------------------------------

REDACTION_CASES = [
    # (輸入, 期望輸出)
    (
        "GET /api/me/ HTTP/1.1 Authorization: Bearer sk_live_abc123XYZ",
        "GET /api/me/ HTTP/1.1 Authorization: [REDACTED]",
    ),
    (
        '{"headers": {"Authorization": "Bearer abc"}, "path": "/api/me/"}',
        '{"headers": {"Authorization": "[REDACTED]"}, "path": "/api/me/"}',
    ),
    ("auth=Bearer tok123,next=1", "auth=[REDACTED],next=1"),
    ("auth=Bearer tok123;next=1", "auth=[REDACTED];next=1"),
    ("auth='Bearer tok123' done", "auth='[REDACTED]' done"),
    ("Authorization: Bearer\ttab_separated_token end", "Authorization: [REDACTED] end"),
    (
        "token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.SflKxw-RJ_adQssw5c rest",
        "token=[REDACTED] rest",
    ),
    ("user alice.smith+tag@example.co.uk logged in", "user [REDACTED] logged in"),
    ('{"email": "bob@example.com", "ok": true}', '{"email": "[REDACTED]", "ok": true}'),
    (
        '203.0.113.7 - - [01/Oct/2026:10:00:00 +0000] "GET /healthz/ HTTP/1.1" 200 2 "-" "curl/8"',
        '203.0.113.7 - - [01/Oct/2026:10:00:00 +0000] "GET /healthz/ HTTP/1.1" 200 2 "-" "curl/8"',
    ),
    (
        "ip=10.0.0.1 email=bob@example.com Authorization: Bearer xyz jwt=eyJa.eyJb.c-d next",
        "ip=10.0.0.1 email=[REDACTED] Authorization: [REDACTED] jwt=[REDACTED] next",
    ),
    (
        "a@b.io and c@d.io both, Bearer one;Bearer two",
        "[REDACTED] and [REDACTED] both, [REDACTED];[REDACTED]",
    ),
    # 不含機密的行:逐位元組不變
    (
        "2026/10/01 10:00:00 [error] 12#12: *3 connect() failed (111: Connection refused) "
        "while connecting to upstream, client: 198.51.100.4, server: api.example.com, "
        'request: "GET /api/events/ HTTP/1.1", upstream: "http://127.0.0.1:8000/api/events/"',
        "2026/10/01 10:00:00 [error] 12#12: *3 connect() failed (111: Connection refused) "
        "while connecting to upstream, client: 198.51.100.4, server: api.example.com, "
        'request: "GET /api/events/ HTTP/1.1", upstream: "http://127.0.0.1:8000/api/events/"',
    ),
    (
        '[2026-10-01 10:00:00 +0000] [7] [INFO] Booting worker with pid: 7 中文 \t "q" \\ end',
        '[2026-10-01 10:00:00 +0000] [7] [INFO] Booting worker with pid: 7 中文 \t "q" \\ end',
    ),
    (
        '{"level": "INFO", "event_id": 42, "ratio": 0.5}',
        '{"level": "INFO", "event_id": 42, "ratio": 0.5}',
    ),
]

HARNESS = """\
logging {
  level  = "info"
  format = "json"
}

import.file "redact" {
  filename = "/etc/alloy/redact.alloy"
}

loki.source.file "samples" {
  targets    = [{"__path__" = "/samples/sample.log"}]
  forward_to = [redact.secrets.test.receiver]
}

redact.secrets "test" {
  forward_to = [loki.echo.out.receiver]
}

loki.echo "out" { }
"""


@pytest.fixture(scope="module")
def redacted(tmp_path_factory):
    docker = _docker_or_skip()
    tmp = tmp_path_factory.mktemp("alloy_redact")
    (tmp / "samples").mkdir()
    lines = [inp for inp, _ in REDACTION_CASES]
    for line in lines:
        assert "\n" not in line
    (tmp / "samples" / "sample.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp / "harness.alloy").write_text(HARNESS)
    name = f"alloy-redact-test-{uuid.uuid4().hex[:8]}"
    try:
        _start_redact_harness(docker, tmp, name)
        return _collect_echo(docker, name, len(lines))
    finally:
        subprocess.run([docker, "rm", "-f", name], capture_output=True, timeout=60)


def _start_redact_harness(docker, tmp, name):
    subprocess.run(
        [
            docker,
            "run",
            "-d",
            "--name",
            name,
            *_bind(REDACT, "/etc/alloy/redact.alloy"),  # 正式檔案本身
            *_bind(tmp / "harness.alloy", "/etc/alloy/harness.alloy"),
            *_bind(tmp / "samples", "/samples"),
            IMAGE,
            "run",
            "/etc/alloy/harness.alloy",
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )


def _collect_echo(docker, name, expected_count):
    deadline = time.time() + 60
    entries: list[str] = []
    logs = ""
    while time.time() < deadline:
        logs = subprocess.run(
            [docker, "logs", name], capture_output=True, text=True, timeout=30
        ).stderr
        entries = []
        for raw in logs.splitlines():
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if rec.get("component_id") == "loki.echo.out" and "entry" in rec:
                entries.append(rec["entry"])
        if len(entries) >= expected_count:
            break
        time.sleep(1)
    assert len(entries) == expected_count, f"只收到 {len(entries)} 筆:\n{logs[-4000:]}"
    return entries


@pytest.mark.parametrize("index", range(len(REDACTION_CASES)))
def test_redaction_sample(redacted, index):
    _, expected = REDACTION_CASES[index]
    assert redacted[index] == expected


def test_config_alloy_does_not_define_its_own_replace_rules():
    # 遮蔽規則只存在 redact.alloy 一處,config.alloy 只匯入使用
    assert "stage.replace" not in CONFIG.read_text()
    assert "stage.replace" in REDACT.read_text()
