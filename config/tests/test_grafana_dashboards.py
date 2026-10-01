"""Grafana dashboard JSON(add-observability-stack design.md D10,task 4.1)。

`infra/grafana/dashboards/` 的 App、Containers、Host、Logs 四張 dashboard 由使用者在
Grafana Cloud 以 Import 匯入。這裡守住:

- JSON 可匯入(必要欄位、uid 穩定且唯一、沒有 `id`)。
- data source 只透過 dashboard 的 `datasource` 類型變數指定,不寫死 stack 的 uid。
- App 的查詢排除 `view="metrics"`(Alloy scrape 本身的流量)。
- 查詢用到的 metric / label 名稱都是實際會被收集的:
  - django-prometheus:從安裝的套件註冊出來的 metric 推導(不手抄清單)。
  - cadvisor:解析 `config.alloy` 的 relabel 白名單與 labeldrop。
  - node:`config.alloy` 的 `set_collectors` 有開的 collector 才可用。
  - Loki:stream label 只有 `config.alloy` 設定的 `container` / `source` / `service_name`
    / `log_type`(`service_name` 對 docker log 由 Grafana Cloud 從 `container` 推導)。
- JSON 不含 token、Grafana Cloud 網址或 stack 專屬 uid。
- (docker)以真正的 Grafana 走 Import API 匯入成功;docker 不存在時本機 skip、CI 直接
  fail(同 test_prod_compose.py)。
"""

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASH_DIR = REPO_ROOT / "infra" / "grafana" / "dashboards"
ALLOY_CONFIG = REPO_ROOT / "infra" / "alloy" / "config.alloy"

# 檔名 -> 需要的 data source 類型
DASHBOARDS = {
    "app.json": {"prometheus"},
    "containers.json": {"prometheus"},
    "host.json": {"prometheus"},
    "logs.json": {"loki"},
}

GRAFANA_IMAGE = "grafana/grafana:12.2.0"
MIN_SCHEMA_VERSION = 39
AI_RECOMMENDATION_VIEW = "events:restaurant-recommendations"


# --- helpers -----------------------------------------------------------------


def _load(name):
    return json.loads((DASH_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module", params=sorted(DASHBOARDS))
def dashboard(request):
    path = DASH_DIR / request.param
    assert path.is_file(), f"缺少 {path.relative_to(REPO_ROOT)}"
    return request.param, json.loads(path.read_text(encoding="utf-8"))


def _panels(dash):
    """攤平 row 內的 panel;row 本身不算(沒有查詢)。"""
    out = []
    for p in dash.get("panels", []):
        if p.get("type") == "row":
            out.extend(p.get("panels", []))
        else:
            out.append(p)
    return out


def _targets(dash):
    for p in _panels(dash):
        for t in p.get("targets", []):
            yield p, t


def _walk_datasources(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "datasource":
                yield v
            yield from _walk_datasources(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_datasources(v)


def _ds_variables(dash):
    return {
        v["name"]: v
        for v in dash.get("templating", {}).get("list", [])
        if v.get("type") == "datasource"
    }


VAR_REF = re.compile(r"^\$\{(\w+)\}$")


def _ds_var_name(ds):
    uid = ds.get("uid") if isinstance(ds, dict) else ds
    m = VAR_REF.match(uid or "")
    return m.group(1) if m else None


# --- PromQL 解析(只需要找出 metric 與 label 名稱) ---------------------------

PROMQL_KEYWORDS = {
    "by", "without", "on", "ignoring", "group_left", "group_right", "bool",
    "and", "or", "unless", "offset", "inf", "nan",
}
LABEL_LIST = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\(([^)]*)\)")
SELECTOR = re.compile(r"([a-zA-Z_:][a-zA-Z0-9_:]*)?\s*\{([^}]*)\}")
MATCHER = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*\"((?:[^\"\\]|\\.)*)\"")


def _strip_promql(expr):
    s = re.sub(r"\"(?:[^\"\\]|\\.)*\"", '""', expr)  # 字串
    s = re.sub(r"\[[^\]]*\]", "", s)  # range / subquery
    s = re.sub(r"\{[^}]*\}", "", s)  # matcher
    s = LABEL_LIST.sub("", s)
    s = re.sub(r"\$\{?\w+\}?", "", s)  # Grafana 變數($__rate_interval 等)
    return s


def promql_metrics(expr):
    s = _strip_promql(expr)
    names = set()
    for m in re.finditer(r"[a-zA-Z_:][a-zA-Z0-9_:]*", s):
        name = m.group(0)
        rest = s[m.end():].lstrip()
        if rest.startswith("("):
            continue  # 函式 / 聚合
        if name.lower() in PROMQL_KEYWORDS:
            continue
        if re.fullmatch(r"\d+(?:ms|s|m|h|d|w|y)?", name) or re.fullmatch(r"e\d*", name):
            continue
        prev = s[: m.start()]
        if prev and (prev[-1].isdigit() or prev[-1] == "."):
            continue  # 1e3 之類的數字
        names.add(name)
    return names


def promql_labels(expr):
    labels = set()
    for m in SELECTOR.finditer(expr):
        labels.update(x.group(1) for x in MATCHER.finditer(m.group(2)))
    for m in LABEL_LIST.finditer(expr):
        labels.update(x.strip() for x in m.group(1).split(",") if x.strip())
    return labels


def promql_selectors(expr):
    """(metric, {label: [(op, value), ...]}),只取有 metric 名稱的 selector。"""
    out = []
    for m in SELECTOR.finditer(expr):
        if m.group(1):
            matchers = {}
            for x in MATCHER.finditer(m.group(2)):
                matchers.setdefault(x.group(1), []).append((x.group(2), x.group(3)))
            out.append((m.group(1), matchers))
    return out


def unparsed_matchers(expr):
    """selector 內沒被 MATCHER 認出的片段(單引號、反引號等)——有的話上面的檢查會被略過。"""
    bad = []
    for m in SELECTOR.finditer(expr):
        rest = MATCHER.sub("", m.group(2))
        if rest.replace(",", "").strip():
            bad.append(m.group(0))
    return bad


def test_promql_parser_self_check():
    expr = (
        'histogram_quantile(0.95, sum by (le, view) (rate('
        'django_http_requests_latency_seconds_by_view_method_bucket{view!="metrics"}[$__rate_interval])))'
        ' / on(view) group_left foo_total offset 5m > 1e3'
    )
    assert promql_metrics(expr) == {
        "django_http_requests_latency_seconds_by_view_method_bucket",
        "foo_total",
    }
    assert promql_labels(expr) == {"le", "view"}
    assert promql_selectors(expr)[0][1] == {"view": [("!=", "metrics")]}
    two = promql_selectors('a{view="x", view!="metrics"}')
    assert two == [("a", {"view": [("=", "x"), ("!=", "metrics")]})]
    assert unparsed_matchers("a{view='metrics'}") == ["a{view='metrics'}"]
    assert unparsed_matchers('a{view!="metrics", b=~"x|y"}') == []


# --- 實際輸出的 metric / label(由來源推導) ----------------------------------

COMMON_LABELS = {"job", "instance"}


def _django_metrics():
    """從安裝的 django-prometheus 實際註冊 metric(用獨立 registry,不污染全域)。"""
    from prometheus_client import CollectorRegistry, Counter, Histogram
    from django_prometheus.middleware import Metrics

    registry = CollectorRegistry()
    created = []

    class Capture(Metrics):
        def register_metric(self, metric_cls, name, documentation, labelnames=(), **kwargs):
            m = metric_cls(name, documentation, labelnames=labelnames, registry=registry, **kwargs)
            created.append((metric_cls, m))
            return m

    Capture()
    allowed = {}
    for cls, m in created:
        labels = set(m._labelnames) | COMMON_LABELS
        if issubclass(cls, Counter):
            allowed[m._name + "_total"] = labels
        elif issubclass(cls, Histogram):
            for suffix in ("_bucket", "_sum", "_count"):
                allowed[m._name + suffix] = labels | ({"le"} if suffix == "_bucket" else set())
    return allowed


def _alloy_block(kind, label):
    text = ALLOY_CONFIG.read_text(encoding="utf-8")
    start = text.index(f'{kind} "{label}" {{')
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"config.alloy 的 {kind} {label} 沒有結尾")


def _cadvisor_metrics():
    block = _alloy_block("prometheus.relabel", "cadvisor")
    keep = re.search(
        r'action\s*=\s*"keep"\s*source_labels\s*=\s*\["__name__"\]\s*regex\s*=\s*"([^"]+)"',
        block,
    )
    assert keep, "config.alloy 的 cadvisor relabel 找不到 __name__ keep 白名單"
    drop = re.search(r'action\s*=\s*"labeldrop"\s*regex\s*=\s*"([^"]+)"', block)
    assert drop
    # cadvisor 的 label 由 exporter 本身決定、config.alloy 只能刪(無法從設定推導):
    # 容器名稱 `name`(prod 已確認)、網路的 `interface`、cpu 的 `cpu`;
    # 被 labeldrop 的不可用。
    labels = {"name", "interface", "cpu"} | COMMON_LABELS
    labels = {lbl for lbl in labels if not re.fullmatch(drop.group(1), lbl)}
    return {name: labels for name in keep.group(1).split("|")}


# node_exporter 各 collector 會輸出的 metric(本專案用得到的部分)與 label。
NODE_COLLECTOR_METRICS = {
    "cpu": {"node_cpu_seconds_total": {"cpu", "mode"}},
    "meminfo": {
        f"node_memory_{f}_bytes": set()
        for f in ("MemTotal", "MemAvailable", "MemFree", "Buffers", "Cached", "SwapTotal", "SwapFree")
    },
    "filesystem": {
        f"node_filesystem_{f}": {"device", "fstype", "mountpoint"}
        for f in ("size_bytes", "avail_bytes", "free_bytes")
    },
    "netdev": {
        f"node_network_{d}_{f}_total": {"device"}
        for d in ("receive", "transmit")
        for f in ("bytes", "packets", "errs", "drop")
    },
    "loadavg": {"node_load1": set(), "node_load5": set(), "node_load15": set()},
}


def _node_metrics():
    block = _alloy_block("prometheus.exporter.unix", "host")
    m = re.search(r"set_collectors\s*=\s*\[([^\]]*)\]", block)
    assert m
    collectors = re.findall(r'"([^"]+)"', m.group(1))
    allowed = {}
    for c in collectors:
        for name, labels in NODE_COLLECTOR_METRICS.get(c, {}).items():
            allowed[name] = labels | COMMON_LABELS
    return allowed


@pytest.fixture(scope="module")
def allowed_metrics():
    # `up` 由每個 prometheus.scrape 自己產生;cadvisor 的會被它的 keep 白名單 drop,
    # app 與 host 的直接送出。
    allowed = {"up": set(COMMON_LABELS)}
    allowed.update(_django_metrics())
    allowed.update(_cadvisor_metrics())
    allowed.update(_node_metrics())
    return allowed


def test_allowlist_derived_from_sources(allowed_metrics):
    """推導本身要抓得到 prod 已確認存在的 metric,否則推導壞了而非 dashboard 對了。"""
    for name in (
        "django_http_requests_total_by_view_transport_method_total",
        "django_http_requests_latency_seconds_by_view_method_bucket",
        "django_http_responses_total_by_status_view_method_total",
        "container_memory_working_set_bytes",
        "container_cpu_usage_seconds_total",
        "node_memory_MemAvailable_bytes",
        "node_memory_SwapFree_bytes",
    ):
        assert name in allowed_metrics, name
    assert "container_memory_cache" not in allowed_metrics  # 不在白名單
    assert "node_disk_read_bytes_total" not in allowed_metrics  # diskstats 未開
    assert "image" not in allowed_metrics["container_memory_working_set_bytes"]


LOKI_STREAM_LABELS = {"container", "service_name", "source", "log_type"}


def _loki_stream_labels():
    text = ALLOY_CONFIG.read_text(encoding="utf-8")
    labels = set(re.findall(r'target_label\s*=\s*"(\w+)"', _alloy_block("discovery.relabel", "docker_logs")))
    nginx = _alloy_block("loki.source.file", "nginx")
    labels |= {k for k in re.findall(r'"(\w+)"\s*=', nginx) if not k.startswith("__")}
    assert "loki.source.docker" in text
    # docker log 的 service_name 不是 Alloy 設的:Grafana Cloud 由 `container` 推導
    # (2026-10-01 prod 確認 = container 名稱);nginx 的 service_name 由 config.alloy 設定。
    return labels | {"service_name"}


def test_loki_stream_labels_match_config_alloy():
    assert _loki_stream_labels() == LOKI_STREAM_LABELS


# --- 結構 --------------------------------------------------------------------


def test_all_dashboard_files_exist():
    missing = [n for n in DASHBOARDS if not (DASH_DIR / n).is_file()]
    assert not missing, f"缺少 dashboard:{missing}"
    extra = sorted(p.name for p in DASH_DIR.glob("*.json") if p.name not in DASHBOARDS)
    assert not extra, f"未納入測試的 dashboard:{extra}"


def test_required_top_level_fields(dashboard):
    name, d = dashboard
    assert isinstance(d.get("title"), str) and d["title"].strip()
    assert isinstance(d.get("uid"), str) and re.fullmatch(r"[a-z0-9-]{1,40}", d["uid"]), d.get("uid")
    assert isinstance(d.get("schemaVersion"), int) and d["schemaVersion"] >= MIN_SCHEMA_VERSION
    assert d.get("id") is None, "匯入用 JSON 不可帶 id"
    assert isinstance(d.get("panels"), list) and _panels(d)
    assert d.get("time", {}).get("from") == "now-6h"
    assert d.get("time", {}).get("to") == "now"
    assert "__inputs" not in d, "不使用 __inputs,data source 由 dashboard 變數選"


def test_uids_and_titles_unique():
    ds = [_load(n) for n in DASHBOARDS]
    assert len({d["uid"] for d in ds}) == len(ds)
    assert len({d["title"] for d in ds}) == len(ds)


def test_panel_ids_unique_and_grid_pos(dashboard):
    _, d = dashboard
    ids = [p.get("id") for p in d["panels"]] + [
        c.get("id") for p in d["panels"] if p.get("type") == "row" for c in p.get("panels", [])
    ]
    assert all(isinstance(i, int) for i in ids)
    assert len(ids) == len(set(ids))
    for p in _panels(d):
        g = p.get("gridPos", {})
        assert {"h", "w", "x", "y"} <= g.keys(), p.get("title")
        assert g["x"] + g["w"] <= 24, p.get("title")


def test_every_panel_has_query(dashboard):
    _, d = dashboard
    for p in _panels(d):
        assert p.get("title"), p
        targets = p.get("targets", [])
        assert targets, f"panel {p.get('title')!r} 沒有查詢"
        ref_ids = [t.get("refId") for t in targets]
        assert all(ref_ids) and len(ref_ids) == len(set(ref_ids)), p["title"]
        for t in targets:
            assert isinstance(t.get("expr"), str) and t["expr"].strip(), p["title"]


def test_numeric_panels_have_unit(dashboard):
    _, d = dashboard
    for p in _panels(d):
        if p.get("type") in {"timeseries", "stat", "gauge", "bargauge"}:
            assert p.get("fieldConfig", {}).get("defaults", {}).get("unit"), p["title"]


# --- data source -------------------------------------------------------------


def test_datasource_variables_defined(dashboard):
    name, d = dashboard
    variables = _ds_variables(d)
    assert {v["query"] for v in variables.values()} == DASHBOARDS[name]
    for v in variables.values():
        # current 若帶值會把某個 stack 的 uid 存進 JSON
        assert not v.get("current"), f"變數 {v['name']} 不可帶 current(stack 專屬)"
        assert not v.get("options"), v["name"]


def test_every_datasource_reference_is_a_variable(dashboard):
    _, d = dashboard
    variables = _ds_variables(d)
    refs = list(_walk_datasources(d.get("panels", [])))
    assert refs
    for ds in refs:
        var = _ds_var_name(ds)
        assert var in variables, f"datasource {ds!r} 不是 dashboard 變數"
        if isinstance(ds, dict) and "type" in ds:
            assert ds["type"] == variables[var]["query"], ds
    for p in _panels(d):
        assert p.get("datasource"), p["title"]
        pvar = _ds_var_name(p["datasource"])
        for t in p["targets"]:
            assert _ds_var_name(t.get("datasource")) == pvar, (p["title"], t)


def test_no_annotation_or_other_datasource(dashboard):
    _, d = dashboard
    variables = _ds_variables(d)
    for ds in _walk_datasources(d):
        assert _ds_var_name(ds) in variables, ds


# --- 查詢內容 ----------------------------------------------------------------


def _queries(d, ds_type):
    variables = _ds_variables(d)
    for p, t in _targets(d):
        if variables[_ds_var_name(p["datasource"])]["query"] == ds_type:
            yield p, t["expr"]


def test_prometheus_metric_names_exist(dashboard, allowed_metrics):
    _, d = dashboard
    for p, expr in _queries(d, "prometheus"):
        metrics = promql_metrics(expr)
        assert metrics, (p["title"], expr)
        unknown = metrics - allowed_metrics.keys()
        assert not unknown, f"{p['title']}: 不存在的 metric {unknown}"


def test_prometheus_label_names_exist(dashboard, allowed_metrics):
    _, d = dashboard
    for p, expr in _queries(d, "prometheus"):
        metrics = promql_metrics(expr) & allowed_metrics.keys()
        known = set().union(*(allowed_metrics[m] for m in metrics)) if metrics else set()
        unknown = promql_labels(expr) - known
        assert not unknown, f"{p['title']}: 不存在的 label {unknown}"
        assert not unparsed_matchers(expr), f"{p['title']}: matcher 只用雙引號 {unparsed_matchers(expr)}"
        for metric, matchers in promql_selectors(expr):
            bad = matchers.keys() - allowed_metrics.get(metric, set())
            assert not bad, f"{p['title']}: {metric} 沒有 label {bad}"


def test_app_queries_exclude_metrics_view():
    d = _load("app.json")
    for p, expr in _queries(d, "prometheus"):
        selectors = [s for s in promql_selectors(expr) if s[0].startswith("django_http_")]
        assert selectors, (p["title"], expr)
        assert len(selectors) == len(re.findall(r"django_http_\w+", expr)), (
            f"{p['title']}: 每個 django_http_* 都要帶 matcher"
        )
        for metric, matchers in selectors:
            assert ("!=", "metrics") in matchers.get("view", []), f'{p["title"]}: {metric} 沒有 view!="metrics"'


def _url_view_names():
    from django.urls import URLPattern, URLResolver, get_resolver

    names = set()

    def walk(patterns, ns):
        for p in patterns:
            if isinstance(p, URLResolver):
                child = ns + [p.namespace] if p.namespace else ns
                walk(p.url_patterns, child)
            elif isinstance(p, URLPattern) and p.name:
                names.add(":".join(ns + [p.name]))

    walk(get_resolver().url_patterns, [])
    return names


def test_app_view_label_values_are_real_views():
    names = _url_view_names()
    assert "healthz" in names and AI_RECOMMENDATION_VIEW in names
    d = _load("app.json")
    for p, expr in _queries(d, "prometheus"):
        for _, matchers in promql_selectors(expr):
            for op, value in matchers.get("view", []):
                if op == "=":
                    assert value in names, f"{p['title']}: view {value!r} 不存在"


def test_app_panels_cover_d10():
    d = _load("app.json")
    exprs = {p["title"]: [t["expr"] for t in p["targets"]] for p in _panels(d)}
    flat = "\n".join(e for es in exprs.values() for e in es)
    assert "django_http_requests_total_by_view_transport_method_total" in flat
    assert re.search(r"histogram_quantile\(\s*0\.5\s*,", flat)
    assert re.search(r"histogram_quantile\(\s*0\.95\s*,", flat)
    assert re.search(r'status=~"5\.\."', flat) and re.search(r'status=~"4\.\."', flat)
    ai = [
        title for title, es in exprs.items()
        if any(f'view="{AI_RECOMMENDATION_VIEW}"' in e and re.search(r"histogram_quantile\(\s*0\.95", e) for e in es)
    ]
    assert ai, "缺少 AI 推薦 view 的 p95 latency panel"


def test_containers_panels_cover_d10():
    flat = "\n".join(t["expr"] for _, t in _targets(_load("containers.json")))
    for m in (
        "container_cpu_usage_seconds_total",
        "container_memory_working_set_bytes",
        "container_start_time_seconds",
        "container_network_receive_bytes_total",
        "container_network_transmit_bytes_total",
    ):
        assert m in flat, m
    assert "by (name)" in flat


def test_host_panels_cover_d10():
    d = _load("host.json")
    flat = "\n".join(t["expr"] for _, t in _targets(d))
    for m in (
        "node_cpu_seconds_total",
        "node_memory_MemAvailable_bytes",
        "node_memory_SwapTotal_bytes",
        "node_memory_SwapFree_bytes",
        "node_filesystem_avail_bytes",
        "node_network_receive_bytes_total",
    ):
        assert m in flat, m
    for _, t in _targets(d):
        e = t["expr"]
        for metric, matchers in promql_selectors(e):
            if metric.startswith("node_filesystem"):
                assert any(
                    op == "!~" and "tmpfs" in v and "overlay" in v for op, v in matchers.get("fstype", [])
                ), e
            if metric.startswith("node_network"):
                assert any(
                    op == "!~" and all(x in v for x in ("lo", "veth", "docker"))
                    for op, v in matchers.get("device", [])
                ), e


def test_loki_queries_use_real_stream_labels():
    d = _load("logs.json")
    queries = list(_queries(d, "loki"))
    assert queries
    for p, expr in queries:
        selectors = re.findall(r"\{([^}]*)\}", expr)
        assert selectors, (p["title"], expr)
        for sel in selectors:
            labels = {m.group(1) for m in MATCHER.finditer(sel)}
            assert labels, (p["title"], sel)
            assert labels <= LOKI_STREAM_LABELS, f"{p['title']}: 不存在的 stream label {labels - LOKI_STREAM_LABELS}"


def test_logs_panels_cover_d10():
    d = _load("logs.json")
    panels = _panels(d)
    flat = "\n".join(t["expr"] for _, t in _targets(d))
    assert re.search(r"\(\?i\)", flat), "錯誤 log 比對需不分大小寫"
    assert 'service_name="nginx"' in flat
    assert re.search(r"status\s*=~\s*\"5", flat) or re.search(r"status\s*>=\s*500", flat)
    assert any(p["type"] == "logs" for p in panels)


# --- 機密 / stack 專屬資訊 -----------------------------------------------------


def test_no_secrets_or_stack_specific_values(dashboard):
    name, d = dashboard
    raw = (DASH_DIR / name).read_text(encoding="utf-8")
    assert "glc_" not in raw
    assert "grafana.net" not in raw
    assert not re.search(r"[0-9a-fA-F]{32,}", raw), "像 hex token"
    assert not re.search(r"[A-Za-z0-9+/]{40,}={0,2}", raw), "像 base64 token"
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", raw), "不可含 email"
    assert not re.search(r"grafanacloud-", raw), "不可含 stack 專屬 data source 名稱"


# --- 真正的 Grafana 匯入 -------------------------------------------------------


def _docker_or_skip():
    docker = shutil.which("docker")
    if docker is None:
        if os.environ.get("CI"):
            pytest.fail("CI 環境缺少 docker CLI,無法以真正的 Grafana 驗證匯入")
        pytest.skip("docker CLI 不存在,無法以真正的 Grafana 驗證匯入")
    return docker


def _api(base, path, payload=None):
    auth = base64.b64encode(b"admin:admin").decode()
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


@pytest.fixture(scope="module")
def grafana():
    docker = _docker_or_skip()
    name = f"test-grafana-dash-{uuid.uuid4().hex[:8]}"
    try:
        subprocess.run(
            [docker, "run", "-d", "--name", name, "-p", "127.0.0.1::3000", GRAFANA_IMAGE],
            check=True, capture_output=True, text=True, timeout=300,
        )
        port = subprocess.run(
            [docker, "port", name, "3000"], check=True, capture_output=True, text=True, timeout=30
        ).stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 120
        while True:
            try:
                status, _ = _api(base, "/api/health")
                if status == 200:
                    break
            except (urllib.error.URLError, ConnectionError, socket.timeout, OSError):
                pass
            if time.monotonic() > deadline:
                logs = subprocess.run([docker, "logs", name], capture_output=True, text=True).stderr
                pytest.fail(f"Grafana 未就緒:{logs[-2000:]}")
            time.sleep(1)
        # 與 Grafana Cloud 相同:各有一個 prometheus 與 loki data source 可選。
        for ds_type in ("prometheus", "loki"):
            status, body = _api(
                base, "/api/datasources",
                {"name": f"test-{ds_type}", "type": ds_type, "access": "proxy", "url": "http://127.0.0.1:1"},
            )
            assert status == 200, body
        yield base
    finally:
        subprocess.run([docker, "rm", "-f", name], capture_output=True, timeout=60)


@pytest.mark.parametrize("name", sorted(DASHBOARDS))
def test_imports_into_real_grafana(grafana, name):
    d = _load(name)
    # 與 UI 的 Import 相同的 API
    status, body = _api(grafana, "/api/dashboards/import", {"dashboard": d, "overwrite": True, "inputs": []})
    assert status == 200, body
    status, saved = _api(grafana, f"/api/dashboards/uid/{d['uid']}")
    assert status == 200, saved
    saved_dash = saved["dashboard"]
    assert saved_dash["title"] == d["title"]
    assert len(_panels(saved_dash)) == len(_panels(d))
    assert set(_ds_variables(saved_dash)) == set(_ds_variables(d))


# --- 與 D10 告警對齊(code-review 後補) ----------------------------------------


def _threshold_steps(panel):
    d = panel["fieldConfig"]["defaults"]
    assert d.get("custom", {}).get("thresholdsStyle", {}).get("mode") in {"line", "dashed", "line+area"}, panel["title"]
    return [s["value"] for s in d["thresholds"]["steps"] if s.get("value") is not None]


def _panel_with(d, needle):
    found = [p for p in _panels(d) if any(needle in t["expr"] for t in p["targets"])]
    assert found, needle
    return found[0]


def test_alert_thresholds_drawn():
    """D10 #2 MemAvailable < 100MB、#3 5xx > 5%:dashboard 上畫出同一條線。"""
    assert 100 * 1024**2 in _threshold_steps(_panel_with(_load("host.json"), "node_memory_MemAvailable_bytes"))
    assert 0.05 in _threshold_steps(_panel_with(_load("app.json"), 'status=~"5..'))


def test_restart_window_matches_alert():
    """D10 #4:15 分鐘內任一 container 重啟。"""
    flat = "\n".join(t["expr"] for _, t in _targets(_load("containers.json")))
    assert "changes(container_start_time_seconds[15m])" in flat
    assert "[1h]" not in flat


def test_scrape_health_panel():
    """D10 #1 收不到 metrics:dashboard 上看得到各 scrape job 的 up。"""
    panel = _panel_with(_load("host.json"), "up")
    assert any(re.search(r"\bup\b", t["expr"]) and "by (job)" in t["expr"] for t in panel["targets"])
