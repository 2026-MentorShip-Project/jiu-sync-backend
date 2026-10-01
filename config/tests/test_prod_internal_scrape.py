"""prod 設定下放行內部 scrape 的 `/metrics`(add-observability-stack design.md D3
「內部 scrape 放行」、specs/infra/observability「內部收集端以 http 存取」)。

Alloy 以 `http://app:8000/metrics` scrape:不帶 `X-Forwarded-Proto`、`Host: app`。

- `SECURE_REDIRECT_EXEMPT = [r"^metrics$"]`:只有 `/metrics` 不被導向 https,其他路徑
  (含長得像的 `/metricsx`、`/metrics/extra`、`/api/metrics`)仍 301。
- `"app"` 在程式碼附加到 `ALLOWED_HOSTS`(不靠 `.env`),`.env` 原有的 host 不受影響、
  不重複;`DJANGO_ALLOWED_HOSTS` 空白時仍 fail fast(附加 `app` 不可掩蓋缺設定)。

prod.py 在 import 時讀環境變數,所以用 subprocess 以受控的環境載入真正的
`config.settings.prod`,並在同一個 process 內以 Django test Client 發請求,確保測試守住的
是實際的 prod.py 計算結果與 middleware 行為,而不是測試內複製的設定值。
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "s3cr3t-metrics-token"
PUBLIC_HOST = "api.jiu-sync.example.com"

# 在 prod settings 下執行:印出設定值,並依 argv 的請求清單發請求,回傳 JSON。
_PROBE = r"""
import json, sys
import django
django.setup()
from django.conf import settings
from django.test import Client

out = {
    "ALLOWED_HOSTS": list(settings.ALLOWED_HOSTS),
    "SECURE_REDIRECT_EXEMPT": list(getattr(settings, "SECURE_REDIRECT_EXEMPT", [])),
    "SECURE_SSL_REDIRECT": settings.SECURE_SSL_REDIRECT,
    "responses": [],
}
client = Client(raise_request_exception=False)
for req in json.loads(sys.argv[1]):
    extra = {"HTTP_HOST": req["host"]}
    if req.get("token") is not None:
        extra["HTTP_AUTHORIZATION"] = f"Bearer {req['token']}"
    if req.get("https"):
        extra["HTTP_X_FORWARDED_PROTO"] = "https"
    r = client.get(req["path"], **extra)
    out["responses"].append({
        "status": r.status_code,
        "location": r.headers.get("Location"),
        "content_type": r.headers.get("Content-Type"),
        "content": r.content.decode("utf-8", "replace"),
        "headers": dict(r.headers),
    })
print("PROBE_JSON=" + json.dumps(out))
"""


def _run_prod(env_overrides, requests=(), unset=()):
    env = {k: v for k, v in os.environ.items() if k not in unset}
    # 受控的 prod 必要變數;明確設定的值優先於 .env(read_env 用 setdefault)。
    env.update(
        {
            "DJANGO_SETTINGS_MODULE": "config.settings.prod",
            "DJANGO_SECRET_KEY": "prod-test-secret-key-not-insecure-0123456789abcdef",
            "FRONTEND_BASE_URL": "https://jiu-sync.example.com",
            "METRICS_TOKEN": TOKEN,
        }
    )
    # 明確設定而非 pop:pop 掉後 base.py 的 read_env(setdefault)會從開發者的 .env 補回。
    env["SECURE_SSL_REDIRECT"] = "True"
    env.update(env_overrides)
    with tempfile.TemporaryDirectory(prefix="prom-mp-") as multiproc_dir:
        env["PROMETHEUS_MULTIPROC_DIR"] = multiproc_dir
        return subprocess.run(
            [sys.executable, "-c", _PROBE, json.dumps(list(requests))],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )


def _probe(env_overrides, requests=()):
    result = _run_prod(env_overrides, requests)
    assert result.returncode == 0, result.stderr
    line = next(ln for ln in result.stdout.splitlines() if ln.startswith("PROBE_JSON="))
    return json.loads(line.removeprefix("PROBE_JSON="))


# ---------------------------------------------------------------- 請求層級(真 prod.py)

_REQUESTS = {
    "metrics_app_token": {"path": "/metrics", "host": "app", "token": TOKEN},
    "metrics_app_port_token": {"path": "/metrics", "host": "app:8000", "token": TOKEN},
    "metrics_app_no_token": {"path": "/metrics", "host": "app"},
    "metrics_app_wrong_token": {"path": "/metrics", "host": "app", "token": "wrong"},
    "metrics_app_port_no_token": {"path": "/metrics", "host": "app:8000"},
    "nonexistent_public_https": {
        "path": "/this-path-does-not-exist-xyz/",
        "host": PUBLIC_HOST,
        "https": True,
    },
    "metrics_public_https_no_token": {"path": "/metrics", "host": PUBLIC_HOST, "https": True},
    "healthz_app": {"path": "/healthz/", "host": "app"},
    "healthz_app_port": {"path": "/healthz/", "host": "app:8000"},
    "api_me_app": {"path": "/api/me/", "host": "app"},
    "api_me_public_http": {"path": "/api/me/", "host": PUBLIC_HOST},
    "metricsx_app": {"path": "/metricsx", "host": "app", "token": TOKEN},
    "metrics_slash_extra_app": {"path": "/metrics/extra", "host": "app", "token": TOKEN},
    "metrics_trailing_slash_app": {"path": "/metrics/", "host": "app", "token": TOKEN},
    "api_metrics_app": {"path": "/api/metrics", "host": "app", "token": TOKEN},
    "nonexistent_app_http": {"path": "/this-path-does-not-exist-xyz/", "host": "app"},
    "healthz_public_https": {"path": "/healthz/", "host": PUBLIC_HOST, "https": True},
    "healthz_app_https": {"path": "/healthz/", "host": "app", "https": True},
    "metrics_evil_token": {"path": "/metrics", "host": "evil.example", "token": TOKEN},
    "healthz_evil_https": {"path": "/healthz/", "host": "evil.example", "https": True},
}


@pytest.fixture(scope="module")
def prod_responses():
    # DJANGO_ALLOWED_HOSTS 刻意不含 app。
    out = _probe({"DJANGO_ALLOWED_HOSTS": PUBLIC_HOST}, requests=list(_REQUESTS.values()))
    return dict(zip(_REQUESTS, out["responses"], strict=True))


@pytest.mark.parametrize("name", ["metrics_app_token", "metrics_app_port_token"])
def test_internal_http_scrape_with_token_returns_200_metrics(prod_responses, name):
    r = prod_responses[name]
    assert r["status"] == 200, r
    assert r["content_type"].startswith("text/plain")
    assert "django_http_requests" in r["content"]


@pytest.mark.parametrize(
    "name", ["metrics_app_no_token", "metrics_app_wrong_token", "metrics_app_port_no_token"]
)
def test_internal_http_scrape_without_valid_token_is_404_not_301_or_400(prod_responses, name):
    r = prod_responses[name]
    baseline = prod_responses["nonexistent_public_https"]
    assert baseline["status"] == 404
    # 與對外看到的「不存在路徑」相同,不透露端點存在。http 下不存在的路徑一律 301,沒有
    # 同 scheme 的 404 可比,所以比對 https 的;唯一差異是 Strict-Transport-Security——
    # SecurityMiddleware 只在 https 回應加 HSTS(RFC 6797 規定 http 上的 HSTS 無效),
    # 與端點是否存在無關。
    assert r["status"] == baseline["status"]
    assert r["content"] == baseline["content"]
    assert "Strict-Transport-Security" not in r["headers"]
    assert r["headers"] == {
        k: v for k, v in baseline["headers"].items() if k != "Strict-Transport-Security"
    }


def test_public_metrics_without_token_still_same_as_nonexistent_path(prod_responses):
    r = prod_responses["metrics_public_https_no_token"]
    baseline = prod_responses["nonexistent_public_https"]
    assert (r["status"], r["content"], r["headers"]) == (
        baseline["status"],
        baseline["content"],
        baseline["headers"],
    )


@pytest.mark.parametrize(
    "name, expected_location",
    [
        ("healthz_app", "https://app/healthz/"),
        ("healthz_app_port", "https://app:8000/healthz/"),
        ("api_me_app", "https://app/api/me/"),
        ("api_me_public_http", f"https://{PUBLIC_HOST}/api/me/"),
        ("nonexistent_app_http", "https://app/this-path-does-not-exist-xyz/"),
    ],
)
def test_other_paths_over_http_still_redirect_to_https(prod_responses, name, expected_location):
    r = prod_responses[name]
    assert r["status"] == 301, r
    assert r["location"] == expected_location


@pytest.mark.parametrize(
    "name, expected_location",
    [
        ("metricsx_app", "https://app/metricsx"),
        ("metrics_slash_extra_app", "https://app/metrics/extra"),
        ("metrics_trailing_slash_app", "https://app/metrics/"),
        ("api_metrics_app", "https://app/api/metrics"),
    ],
)
def test_exempt_pattern_does_not_match_lookalike_paths(prod_responses, name, expected_location):
    r = prod_responses[name]
    assert r["status"] == 301, r
    assert r["location"] == expected_location


@pytest.mark.parametrize("name", ["healthz_public_https", "healthz_app_https"])
def test_https_requests_on_allowed_hosts_unaffected(prod_responses, name):
    assert prod_responses[name]["status"] == 200


@pytest.mark.parametrize("name", ["metrics_evil_token", "healthz_evil_https"])
def test_unknown_host_still_rejected_with_400(prod_responses, name):
    assert prod_responses[name]["status"] == 400


# ---------------------------------------------------------------- 設定值(真 prod.py)


def test_redirect_exempt_is_exactly_metrics_pattern():
    out = _probe({"DJANGO_ALLOWED_HOSTS": PUBLIC_HOST})
    assert out["SECURE_REDIRECT_EXEMPT"] == [r"^metrics$"]
    assert out["SECURE_SSL_REDIRECT"] is True


@pytest.mark.parametrize(
    "env_value, expected",
    [
        (PUBLIC_HOST, [PUBLIC_HOST, "app"]),
        (
            f"{PUBLIC_HOST},www.jiu-sync.example.com",
            [PUBLIC_HOST, "www.jiu-sync.example.com", "app"],
        ),
        # 已含 app 時不重複
        (f"{PUBLIC_HOST},app", [PUBLIC_HOST, "app"]),
        (f"app,{PUBLIC_HOST}", ["app", PUBLIC_HOST]),
        ("app", ["app"]),
    ],
)
def test_app_appended_to_allowed_hosts_without_duplicates(env_value, expected):
    out = _probe({"DJANGO_ALLOWED_HOSTS": env_value})
    assert out["ALLOWED_HOSTS"] == expected


@pytest.mark.parametrize("env_value", [None, ""])
def test_missing_allowed_hosts_still_fails_fast_despite_app(env_value):
    # 附加 app 不可掩蓋 DJANGO_ALLOWED_HOSTS 未設定。
    overrides = {} if env_value is None else {"DJANGO_ALLOWED_HOSTS": env_value}
    unset = ("DJANGO_ALLOWED_HOSTS",) if env_value is None else ()
    env_has_it = env_value is None and _dotenv_has("DJANGO_ALLOWED_HOSTS")
    if env_has_it:
        pytest.skip(".env 設了 DJANGO_ALLOWED_HOSTS,無法測未設定的情境")
    result = _run_prod(overrides, unset=unset)
    assert result.returncode != 0
    assert "DJANGO_ALLOWED_HOSTS must be set in production" in result.stderr


def _dotenv_has(key):
    dotenv = REPO_ROOT / ".env"
    if not dotenv.exists():
        return False
    return any(
        line.split("=", 1)[0].strip() == key
        for line in dotenv.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
