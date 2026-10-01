"""`/metrics` 端點(add-observability-stack design.md D2/D3,specs/infra/observability)。

- D3:只有 `Authorization: Bearer <METRICS_TOKEN>` 完全相符才回 metrics;未帶、不符、
  scheme 不對、`METRICS_TOKEN` 未設定或空字串一律 `Http404`,回應與不存在的路徑相同
  (走 `config.exceptions.handler404`),比對用 `hmac.compare_digest`。
- D2:django-prometheus 的 Before/After middleware 輸出 view 層級的請求數、latency、
  status;設定 `PROMETHEUS_MULTIPROC_DIR` 時多個 process 的數值加總,worker 被
  mark dead 後 counter 仍保留。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.test import Client
from prometheus_client.parser import text_string_to_metric_families

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "s3cr3t-metrics-token"

REQUESTS_BY_VIEW = "django_http_requests_total_by_view_transport_method"
LATENCY_BY_VIEW = "django_http_requests_latency_seconds_by_view_method"
RESPONSES_BY_STATUS_VIEW = "django_http_responses_total_by_status_view_method"


def _samples(body):
    """把 Prometheus 文字格式解析成 [(sample_name, labels, value)]。"""
    text = body.decode() if isinstance(body, bytes) else body
    return [
        (s.name, s.labels, s.value)
        for family in text_string_to_metric_families(text)
        for s in family.samples
    ]


def _value(samples, name, **labels):
    """回傳名稱與 labels(子集合)都相符的 sample 值總和;找不到回 0。"""
    return sum(
        v
        for n, lbls, v in samples
        if n == name and all(lbls.get(k) == val for k, val in labels.items())
    )


def _scrape(client, token=TOKEN):
    response = client.get("/metrics", HTTP_AUTHORIZATION=f"Bearer {token}")
    assert response.status_code == 200, response.content
    return _samples(response.content)


def _assert_same_as_nonexistent_path(client, response):
    """回應必須與不存在的路徑完全相同(status、body、headers),不透露端點存在。"""
    baseline = client.get("/this-path-does-not-exist-xyz/")
    assert baseline.status_code == 404
    assert response.status_code == baseline.status_code
    assert response.content == baseline.content
    assert dict(response.headers) == dict(baseline.headers)


# ---------------------------------------------------------------- settings 接線


def test_django_prometheus_installed():
    assert "django_prometheus" in django_settings.INSTALLED_APPS


def test_before_middleware_first_and_after_middleware_last():
    assert django_settings.MIDDLEWARE[0] == (
        "django_prometheus.middleware.PrometheusBeforeMiddleware"
    )
    assert django_settings.MIDDLEWARE[-1] == (
        "django_prometheus.middleware.PrometheusAfterMiddleware"
    )


def test_django_prometheus_public_urls_not_included():
    # D3:不使用 django_prometheus.urls 的公開路由(其路徑是 /metrics 且無保護)。
    from django.urls import get_resolver

    def walk(patterns):
        for p in patterns:
            yield p
            yield from walk(getattr(p, "url_patterns", []))

    modules = {
        getattr(p, "urlconf_name", None).__name__
        for p in walk(get_resolver().url_patterns)
        if hasattr(getattr(p, "urlconf_name", None), "__name__")
    }
    assert "django_prometheus.urls" not in modules


def _settings_value_in_subprocess(env_overrides, unset=()):
    env = {k: v for k, v in os.environ.items() if k not in unset}
    env.update(env_overrides)
    env["DJANGO_SETTINGS_MODULE"] = "config.settings.dev"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import django; django.setup(); from django.conf import settings; "
            "print(repr(settings.METRICS_TOKEN))",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_metrics_token_read_from_env():
    assert _settings_value_in_subprocess({"METRICS_TOKEN": "abc123"}) == "'abc123'"


def test_metrics_token_defaults_to_empty_string_when_unset():
    assert _settings_value_in_subprocess({}, unset=("METRICS_TOKEN",)) == "''"


# ---------------------------------------------------------------- 正確 token


def test_correct_token_returns_200_with_prometheus_content_type(settings):
    settings.METRICS_TOKEN = TOKEN
    response = Client().get("/metrics", HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/plain")
    assert REQUESTS_BY_VIEW.encode() in response.content


def test_metrics_include_view_level_requests_latency_and_status(settings):
    settings.METRICS_TOKEN = TOKEN
    client = Client()
    before = _scrape(client)

    for _ in range(3):
        assert client.get("/healthz/").status_code == 200
    assert client.post("/healthz/").status_code == 405

    after = _scrape(client)

    def delta(name, **labels):
        return _value(after, name, **labels) - _value(before, name, **labels)

    assert delta(f"{REQUESTS_BY_VIEW}_total", view="healthz", method="GET") == 3
    assert delta(f"{REQUESTS_BY_VIEW}_total", view="healthz", method="POST") == 1
    assert delta(f"{LATENCY_BY_VIEW}_count", view="healthz", method="GET") == 3
    assert delta(f"{LATENCY_BY_VIEW}_bucket", view="healthz", method="GET", le="+Inf") == 3
    assert (
        delta(f"{RESPONSES_BY_STATUS_VIEW}_total", view="healthz", method="GET", status="200")
        == 3
    )
    assert (
        delta(f"{RESPONSES_BY_STATUS_VIEW}_total", view="healthz", method="POST", status="405")
        == 1
    )


def test_token_compared_with_hmac_compare_digest(settings, monkeypatch):
    import hmac

    import config.metrics as metrics_module

    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(metrics_module.hmac, "compare_digest", spy)
    settings.METRICS_TOKEN = TOKEN
    client = Client()

    assert client.get("/metrics", HTTP_AUTHORIZATION=f"Bearer {TOKEN}").status_code == 200
    assert client.get("/metrics", HTTP_AUTHORIZATION="Bearer wrong").status_code == 404
    # 正確與錯誤 token 都必須經過 compare_digest,不可先用 == 短路。
    assert len(calls) == 2


# ---------------------------------------------------------------- 404 情境


@pytest.mark.parametrize(
    "authorization",
    [
        None,  # 未帶 header
        "",  # 空 header
        "Bearer wrong-token",
        f"Bearer {TOKEN}x",  # 前綴相同但較長
        f"Bearer {TOKEN[:-1]}",  # 前綴相同但較短
        "Bearer ",  # Bearer 但空值
        "Bearer",  # 只有 scheme
        f"Basic {TOKEN}",  # scheme 不對
        f"bearer {TOKEN}",  # scheme 大小寫不同
        f"BEARER {TOKEN}",
        f"Token {TOKEN}",
        TOKEN,  # 沒有 scheme
        f"Bearer  {TOKEN}",  # 多一個空白
        f"Bearer {TOKEN} ",  # 結尾多空白
        f" Bearer {TOKEN}",  # 開頭多空白
        f"Bearer\t{TOKEN}",  # tab 分隔
        f"Bearer {TOKEN.upper()}",  # token 大小寫不同
        "Bearer tökén",  # 非 ASCII,不可讓 compare_digest 丟 TypeError 變 500
    ],
)
def test_wrong_or_missing_token_returns_same_404_as_nonexistent_path(settings, authorization):
    settings.METRICS_TOKEN = TOKEN
    client = Client()
    extra = {} if authorization is None else {"HTTP_AUTHORIZATION": authorization}
    response = client.get("/metrics", **extra)
    _assert_same_as_nonexistent_path(client, response)


@pytest.mark.parametrize("configured", ["", "   "])
@pytest.mark.parametrize(
    "authorization", [None, "Bearer ", "Bearer", "Bearer    ", f"Bearer {TOKEN}"]
)
def test_unconfigured_token_fails_closed_with_same_404(settings, configured, authorization):
    settings.METRICS_TOKEN = configured
    client = Client()
    extra = {} if authorization is None else {"HTTP_AUTHORIZATION": authorization}
    response = client.get("/metrics", **extra)
    _assert_same_as_nonexistent_path(client, response)


def test_missing_metrics_token_setting_fails_closed(settings):
    del settings.METRICS_TOKEN
    client = Client()
    response = client.get("/metrics", HTTP_AUTHORIZATION="Bearer ")
    _assert_same_as_nonexistent_path(client, response)


def test_post_without_token_returns_same_404_not_csrf_403(settings):
    # CsrfViewMiddleware 若先擋下會回 403,等於透露端點存在。
    settings.METRICS_TOKEN = TOKEN
    client = Client(enforce_csrf_checks=True)
    response = client.post("/metrics")
    baseline = client.post("/this-path-does-not-exist-xyz/")
    assert baseline.status_code == 404
    assert response.status_code == 404
    assert response.content == baseline.content


# ---------------------------------------------------------------- multiprocess

_WORKER_SCRIPT = """
import os, sys, django
django.setup()
from django.test import Client
c = Client()
for _ in range(int(sys.argv[1])):
    assert c.get("/healthz/").status_code == 200
print(os.getpid())
"""

_READER_SCRIPT = """
import sys, django
django.setup()
from django.test import Client
r = Client().get("/metrics", HTTP_AUTHORIZATION="Bearer " + sys.argv[1])
assert r.status_code == 200, r.status_code
sys.stdout.write(r.content.decode())
"""


def _run(script, multiproc_dir, *args):
    env = dict(os.environ)
    env.update(
        {
            "DJANGO_SETTINGS_MODULE": "config.settings.dev",
            "PROMETHEUS_MULTIPROC_DIR": str(multiproc_dir),
            "METRICS_TOKEN": TOKEN,
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_multiprocess_counters_are_aggregated_and_survive_mark_process_dead(tmp_path):
    from prometheus_client.multiprocess import mark_process_dead

    pid_a = int(_run(_WORKER_SCRIPT, tmp_path, "3").strip())
    pid_b = int(_run(_WORKER_SCRIPT, tmp_path, "5").strip())
    assert pid_a != pid_b

    # 每個 process 各自寫自己的 .db 檔(確認真的走 multiprocess 模式)。
    db_files = {p.name for p in tmp_path.glob("*.db")}
    assert any(str(pid_a) in name for name in db_files)
    assert any(str(pid_b) in name for name in db_files)

    def healthz_total():
        samples = _samples(_run(_READER_SCRIPT, tmp_path, TOKEN))
        return (
            _value(samples, f"{REQUESTS_BY_VIEW}_total", view="healthz"),
            _value(samples, f"{LATENCY_BY_VIEW}_count", view="healthz"),
            _value(samples, f"{RESPONSES_BY_STATUS_VIEW}_total", view="healthz", status="200"),
        )

    first = healthz_total()
    second = healthz_total()
    assert first == (8, 8, 8)
    assert second == first  # 多次讀取一致,不隨讀取的 process 變動

    mark_process_dead(pid_a, path=str(tmp_path))
    assert healthz_total() == (8, 8, 8)  # worker 死亡後已累計的 counter 仍保留
