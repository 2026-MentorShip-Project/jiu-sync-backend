## Why

正式環境(單台 t2.micro)目前沒有任何 metrics 與集中式 log:API 慢在哪個 view、AI 推薦呼叫 Perplexity(同步、最長 45 秒)是否把 gunicorn 12 個 thread 佔滿、container 是否被 OOM kill、主機記憶體還剩多少,都只能靠 SSM 進去臨場查。實測 baseline(2026-10-01):952MB 總記憶體、available 292MB、無 swap,已經接近極限,需要能持續觀察。

## What Changes

- App 加入 `django-prometheus`,以 gunicorn multiprocess mode 輸出 `/metrics`(request rate、各 view latency、HTTP status)
- 新增 `gunicorn.conf.py`(multiprocess 目錄、worker 結束清理),Dockerfile `CMD` 改用此設定檔,參數不變(3 workers × 4 threads)
- `/metrics` 以 `METRICS_TOKEN` bearer token 保護,未通過一律 404(fail closed);nginx 加第二道 404
- 正式 compose 新增 Grafana Alloy:scrape app `/metrics`、內建 cadvisor(container)與 unix exporter(EC2 host),收集所有 container log 與 host 上的 nginx log,遮蔽敏感字串後推送至 Grafana Cloud Prometheus / Loki
- `deploy.sh` 一併傳送 `config.alloy`、部署後重啟 alloy;缺少 Grafana Cloud 變數只警告不中止
- EC2 新增 1GB swap(人工步驟)
- Grafana Cloud 建 4 張 dashboard 與 4 條告警,JSON 匯出進 repo
- 不改任何既有 API 行為
- (2026-10-02 追加)gunicorn `threads` 4 → 8,同時處理的請求由 12 增為 24(design.md D12)
- (2026-10-02 追加)`apps.*` / `config.*` logger 改為一行一筆 JSON 輸出到 stdout,並在登入、活動狀態變更、投票、通知信 task 加上關鍵事件 log(design.md D11)

未涵蓋(明確排除):root 與第三方 logger 的 JSON 化;Postgres / Redis / PgBouncer / Celery 專屬 exporter;本機 compose 加 Alloy;docker-socket-proxy;Terraform 管理 Grafana 資源;CD pipeline。

## Capabilities

### New Capabilities

- `infra/observability`:app metrics 端點與存取控制、container/host metrics 與 log 收集、敏感資料遮蔽、觀測系統故障不影響 app、dashboard 與告警

### Modified Capabilities

(無)

## Impact

- 新依賴:`django-prometheus`(Python)、`grafana/alloy` image(釘版本)、Grafana Cloud free tier
- `config/settings/base.py`(INSTALLED_APPS、MIDDLEWARE、`METRICS_TOKEN`)、`config/urls.py`(`/metrics`)
- 新增 `gunicorn.conf.py`;`Dockerfile` `CMD`
- `infra/docker/docker-compose.prod.yml`:新增 `alloy` service 與 `alloy_data` volume;app 加 `PROMETHEUS_MULTIPROC_DIR`
- 新增 `infra/alloy/config.alloy`、`infra/nginx/`(現有設定備份)、`infra/grafana/dashboards/`、`infra/grafana/alerts/`
- `infra/scripts/deploy.sh`:傳送 `config.alloy`、Grafana 變數警告、重啟 alloy
- EC2(人工):`.env` 新增 5 個 `GRAFANA_CLOUD_*` 與 `METRICS_TOKEN`;1GB swap;nginx `/metrics` 404
