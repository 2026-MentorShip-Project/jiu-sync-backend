## 1. 環境健檢與前置準備

- [x] 1.1 環境健檢與 baseline:確認 `git status` / `git log` 正常且位在本 change 的分支;以 docker 執行 `grafana/alloy`(選定並記錄要釘的版本 `vX.Y.Z`)的 `--version` 與 `fmt`;`uv add django-prometheus` 之前先確認 PyPI 上的最新版本支援 Django 6.1。用 SSM 在 EC2 執行 `free -m`、`swapon --show`、`docker stats --no-stream`,結果記錄到 design.md Context。驗證:(auto) alloy 的 `--version` 與 `fmt` 指令執行成功;(manual) baseline 已寫入 design.md
  - 結果(2026-10-01):git 正常,位於 `feature/observability`;`grafana/alloy:v1.20.1` 的 `--version` 與 `fmt` 成功(不合法設定 `fmt` 回 exit 1);baseline 已寫入 design.md Context。**待決定**:`django-prometheus` 最新 stable 2.5.0 限制 `Django<6.1`,不支援 Django 6.1.1,只有 `2.6.0.dev*` 預發布版支援(見 design.md D2),Task 2.1 前需決定採用方式。
- [ ] 1.2 建立 Grafana Cloud stack 與只有 `metrics:write`、`logs:write` 權限的 Access Policy token;EC2 `.env` 新增 5 個 `GRAFANA_CLOUD_*` 與 `METRICS_TOKEN`(D5);EC2 建立 1GB swapfile、`vm.swappiness=10`、寫入 `/etc/fstab`(D8)。驗證:(manual) `swapon --show` 有輸出、`sysctl vm.swappiness` 為 10、`.env` 的 6 個變數齊全(只檢查 key 是否存在,不印出 value)

## 2. App 輸出 metrics(django-prometheus + `/metrics` 保護)

- [ ] 2.1 先寫測試(紅燈):`/metrics` 帶正確 token 回 200,未帶 token、token 錯誤、`METRICS_TOKEN` 未設定或空字串都回 404,而且 404 的內容與不存在的路徑相同;metrics 內容包含 view 層級的 request / latency / status;multiprocess 下多個 process 的 counter 會加總、mark dead 之後 counter 仍保留;`gunicorn.conf.py` 的參數與原本 `CMD` 相同、`on_starting` 會清空目錄。實作 D2/D3(依賴、settings、urls、`gunicorn.conf.py`、Dockerfile `CMD`、compose 的 app 加 `PROMETHEUS_MULTIPROC_DIR`)使測試轉綠。驗證:(auto) `pytest` 全綠;(auto) 本機 `docker build` + `docker run`,gunicorn 啟動 3 個 worker,連續 curl `/metrics` 多次,總請求數一致且遞增
- [ ] 2.2 部署與第二道防線:用 SSM 把 EC2 上現有的 nginx site 設定備份到 `infra/nginx/`(去除機密);在 EC2 nginx 加上 `location = /metrics { return 404; }` 並 reload;執行 `deploy.sh`。驗證:(manual) 從外部 `curl https://<domain>/metrics`(帶與不帶 token)都回 404;在 EC2 內以 `docker compose exec app` 帶 token curl `localhost:8000/metrics` 回 200;其他 API 與 `/healthz/` 正常

## 3. Alloy 管線上線

- [ ] 3.1 先寫測試(紅燈):compose 的 alloy service 符合 D5/D8/D9(釘版本、`mem_limit`、所有掛載 `:ro`、`alloy_data` volume、只有 6 個 environment 變數且沒有 `env_file`、沒有 publish port、不出現在任何 `depends_on`),沿用 `config/tests/test_prod_compose.py` 的 `docker compose config` 做法;`config.alloy` 通過 `alloy fmt`;遮蔽規則用樣本 log 驗證 Bearer、JWT、email 會被遮蔽、IP 保留、其他內容不變(D7);`deploy.sh` 會寫入 `config.alloy`、缺少變數時只印 WARNING 不中止、`up -d` 之後會 restart alloy。實作 `infra/alloy/config.alloy`、compose、`deploy.sh` 使測試轉綠。驗證:(auto) `pytest` 全綠
- [ ] 3.2 部署並確認資料:執行 `deploy.sh`。驗證:(manual) Grafana Cloud Explore 查得到 `django_http_*`、`container_*`(各 container)、`node_*`(含 swap),Loki 查得到各 container 與 nginx 的 log;active series < 3k;`docker stats` 顯示 alloy 記憶體在 200MB 以內;再執行一次 `deploy.sh`,確認 nginx log 沒有重複;`docker compose stop alloy` 期間 API 正常,之後再 `start`

## 4. Dashboard 與告警

- [ ] 4.1 在 Grafana Cloud 建立 App、Containers、Host、Logs 四張 dashboard 與 4 條告警(D10),JSON 匯出到 `infra/grafana/dashboards/` 與 `infra/grafana/alerts/`(確認 JSON 不含 token)。驗證:(manual) 四張 dashboard 的每個 panel 都有資料;`docker compose stop alloy` 超過 10 分鐘後收到「收不到 metrics」告警 email,之後再 `start`
