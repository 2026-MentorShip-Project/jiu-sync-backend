## Context

- 正式環境:單台 t2.micro(952MB,無 swap,available 292MB,2026-10-01 實測);compose 有 `app`(gunicorn gthread 3 workers × 4 threads,`--worker-tmp-dir /dev/shm`)、`worker`、`db`、`pgbouncer`、`redis`;nginx 裝在 host,設定不在 repo;只能用 SSM 連線。
  - Baseline(2026-10-01 以 SSM 實測,Task 1.1):`free -m` total 952 / used 660 / free 82 / shared 14 / buff/cache 340 / available 292,Swap 0/0/0;`swapon --show` 無輸出;`docker stats --no-stream`:app 160.2MiB、worker 116.2MiB、db 20.56MiB、redis 5.277MiB、pgbouncer 1.57MiB(limit 皆為 952.8MiB,即未設 `mem_limit`)。
- `deploy.sh` 只把 compose 檔以 base64 寫到 EC2 `/opt/jiu-sync-backend/`,EC2 上沒有 repo。
- image 同時推 `latest` 與 `sha-<7>` tag(`.github/workflows/build-push.yml`)。
- 只有 `apps.recommendations` 輸出 JSON log;`config.exceptions.handler404` 統一 404 格式。
- 動手前確認的不可妥協邊界(grill 前置):① 觀測系統任何故障不影響 app 運作與部署;② log 不重送、不遺漏(冪等);③ 送到第三方的 log 遮蔽敏感資料;④ `/metrics` 不對外;⑤ 跳過 explore(範圍在對話中已收斂)。
- 參考先前專案 Find-Coffee(K8s):Alloy + node exporter + kube-state-metrics + LogQL,沒有 app metrics。

## Goals / Non-Goals

**Goals:** 見 specs/infra/observability。三層 metrics(app / container / host)+ log,全部推到 Grafana Cloud;EC2 只多一個 Alloy container。

**Non-Goals:** 見 proposal「未涵蓋」。另外:不自建 Prometheus / Loki / Grafana;不加 `METRICS_ENABLED` 開關。

## Decisions

### D1. 收集與儲存:單一 Alloy → Grafana Cloud(grill 前討論)

EC2 只跑 Alloy,`prometheus.remote_write` 推 metrics、`loki.write` 推 log 到 Grafana Cloud free tier(10k series、50GB log、14 天)。
- 替代:EC2 自建 Prometheus / Loki / Grafana——至少需要 600MB 記憶體,t2.micro 跑不動;放在本機——看不到 prod、要開對外 port。不採用。

### D2. App metrics:django-prometheus + multiprocess mode(grill Q2)

- `django_prometheus` 加入 `INSTALLED_APPS`,`PrometheusBeforeMiddleware` 放 MIDDLEWARE 最前、`PrometheusAfterMiddleware` 放最後。
- `PROMETHEUS_MULTIPROC_DIR=/dev/shm/prometheus`(compose 的 app `environment` 設定)。`/dev/shm` 是 container 自己的 tmpfs,container 重啟即清空,滿足「重啟不殘留舊數值」。
- 新增 `gunicorn.conf.py`:沿用現有參數(bind、gthread、3 workers、4 threads、timeout 60、`worker_tmp_dir=/dev/shm`);`on_starting` 清空並建立 multiprocess 目錄;`child_exit` 呼叫 `prometheus_client.multiprocess.mark_process_dead(worker.pid)`。Dockerfile `CMD` 改為 `gunicorn -c gunicorn.conf.py config.wsgi:application`。
- 只有 app 設 `PROMETHEUS_MULTIPROC_DIR`,celery worker 不設。
- 不換 `django_prometheus.db.backends.postgresql`(不輸出 DB 查詢次數;Task 2.1 後 opsx:update,使用者確認):換 engine 會讓觀測進入每一條 SQL 的路徑,牽動 PgBouncer transaction pooling、`select_for_update`、`SET LOCAL`,違反 D9「觀測不影響 app」。慢 API 由 view latency 判斷;DB 層數據之後以 Postgres exporter 另開 change。
- 版本相容性(Task 1.1 查 PyPI,2026-10-01):最新 stable `django-prometheus==2.5.0` 宣告 `Django>=4.2,<6.1,!=5.0.*`(classifiers 到 6.0),**不支援本專案的 Django 6.1.1**,直接 `uv add` 會解析失敗;`2.6.0.dev*` 預發布版(最新 `2.6.0.dev22`,2026-09-19)已放寬為 `<6.2` 並列出 Django 6.1。**決定(2026-10-01,使用者確認)**:釘死 `django-prometheus==2.6.0.dev22`(精確版本,不用範圍),使用面僅 middleware + 匯出,由 Task 2.1 測試守住;2.6.0 正式版釋出後另行升級。若 dev22 在 Task 2.1 實測不可用,退回直接用 `prometheus_client` 自寫 view 層級 middleware。替代:等正式版——時程未知;Django 降回 6.0——為監控降框架版本不划算。不採用。
- multiprocess mode 下不提供 `process_*` metrics,container 的 CPU / 記憶體改由 cadvisor 提供(D6)。
- 替代:放在掛 volume 的一般目錄——需要自己寫清理 entrypoint;gunicorn statsd——看不到 Django view。不採用。

### D3. `/metrics` 存取控制:bearer token,fail closed(grill Q7)

- 自訂 view 包住 django-prometheus 的匯出:`METRICS_TOKEN` 未設定或空字串,或 `Authorization` 不是 `Bearer <METRICS_TOKEN>` → `raise Http404`(走既有 `handler404`,回應與不存在的路徑相同);比對用 `hmac.compare_digest`。
- 不使用 `django_prometheus.urls` 的公開路由。
- 內部 scrape 放行(Task 2.1 實測後 opsx:update,使用者確認):Alloy 以 `http://app:8000/metrics` scrape,不帶 `X-Forwarded-Proto` 且 `Host: app`,原本會被 `SECURE_SSL_REDIRECT`(301)與 `ALLOWED_HOSTS`(400)擋下。`prod.py` 設 `SECURE_REDIRECT_EXEMPT = [r"^metrics$"]`,並在程式碼把 `"app"` 附加到 `ALLOWED_HOSTS`(不靠 `.env`,由測試守住)。對外的 `/metrics` 仍由 nginx 404 + token 保護;外部請求必經 nginx 並帶真實網域,`Host: app` 只會出現在 compose 內網。替代:Alloy 送 `X-Forwarded-Proto`/`Host` header 模擬 nginx——Go HTTP client 改 Host 受限,且需在 Alloy 寫死網域;改 `.env` 的 `DJANGO_ALLOWED_HOSTS`——無測試守住。不採用。
- 第二道防線:nginx `location = /metrics { return 404; }`(人工步驟);先把 EC2 上現有 nginx 設定備份到 `infra/nginx/`,作為參考文件。
- 替代:只靠 nginx——設定不在 repo、沒有測試;用來源 IP 判斷——經 docker-proxy 進來的請求和 Alloy 的來源 IP 分不出來。不採用。

### D4. Alloy 設定檔傳送:deploy.sh base64(grill Q4)

- 新增 `infra/alloy/config.alloy` 與 `infra/alloy/redact.alloy`(遮蔽模組,見 D7);`deploy.sh` 用與 compose 檔相同的 base64 手法,在所有中止檢查通過後寫到 `/opt/jiu-sync-backend/`;compose 以 `:ro` 掛到 `/etc/alloy/config.alloy`、`/etc/alloy/redact.alloy`。repo 內 compose 的掛載來源為 `../alloy/…`,`deploy.sh` 傳送時改寫成 `./…`(與 env_file 的改寫相同手法)。
- `up -d` 之後執行 `docker compose restart alloy`:bind mount 內容變動不會觸發 `up -d` 重建 container。
- 之後做 CD 時直接重用 `deploy.sh`。
- 替代:compose `configs.content` 內嵌——`$` 需要跳脫、難讀、無法用 `alloy fmt`;自己 build Alloy image——多一條 pipeline。不採用。

### D5. 機密:最小權限 token + 只傳必要變數(grill Q5)

- Grafana Cloud Access Policy token 只給 `metrics:write`、`logs:write`。
- `.env` 新增 `GRAFANA_CLOUD_PROM_URL`、`GRAFANA_CLOUD_PROM_USER`、`GRAFANA_CLOUD_LOKI_URL`、`GRAFANA_CLOUD_LOKI_USER`、`GRAFANA_CLOUD_API_TOKEN`、`METRICS_TOKEN`。兩個 URL 存完整 push endpoint(Prometheus:`https://prometheus-...grafana.net/api/prom/push`;Loki:`https://logs-...grafana.net/loki/api/v1/push`),`config.alloy` 直接使用、不再拼接路徑;`*_USER` 為各自的數字 instance ID;`METRICS_TOKEN` 以 `openssl rand -hex 32` 產生。
- alloy service 用 `environment: X: ${X}` 只取這 6 個變數(compose 會自動讀同目錄的 `.env` 做變數展開,pgbouncer 已經這樣用),不使用 `env_file`。`config.alloy` 以 `sys.env()` 讀取。
- 替代:`env_file: .env`——Alloy 會拿到 DB 密碼、`SECRET_KEY` 等用不到的機密。不採用。

### D6. 收集範圍與 series 控制(grill Q12/Q16)

- scrape interval 30s。
- `prometheus.scrape "app"`:`app:8000/metrics`,帶 `authorization { type = "Bearer", credentials = sys.env("METRICS_TOKEN") }`。
- `prometheus.exporter.cadvisor`:`docker_only = true`、`enabled_metrics = ["cpu","memory","network"]`(不收用不到的類別,比事後 drop 省記憶體)(原訂的 `housekeeping_interval = "30s"` 在 Alloy v1.20.1 不是可用參數,加上會使整份設定載入失敗,Task 3.1 實測後移除;記憶體是否足夠於 3.2 實測);用 `prometheus.relabel` 白名單保留 `container_cpu_usage_seconds_total`、`container_memory_working_set_bytes`、`container_memory_rss`、`container_network_receive_bytes_total`、`container_network_transmit_bytes_total`、`container_start_time_seconds`、`container_last_seen`,其他全部 drop;同時 drop `id`、`image` 等高基數 label,只保留 container 名稱;drop `name=""`(主機合計的 root cgroup)。
- `prometheus.exporter.unix`:`set_collectors = ["cpu","meminfo","filesystem","netdev","loadavg"]`。不收 `diskstats`(Task 3.1 後 opsx:update):dashboard 用不到磁碟 I/O,且未掛 `/run/udev` 時每次啟動都會產生一行錯誤 log 送進 Loki。
- 不做 Postgres / Redis / PgBouncer / Celery exporter。
- 目標 active series < 3k,部署後人工確認。

### D7. Log 收集與遮蔽(grill Q8/Q9)

- `discovery.docker` + `loki.source.docker`:label `container`(去掉開頭的 `/`)。
- `loki.source.file`:`/var/log/nginx/access.log`、`error.log`,label `source="nginx"`、`service_name="nginx"`、`log_type`(`service_name` 於 Task 3.2 後補:Grafana Drilldown 以 `service_name` 分組,沒有時顯示為 `unknown_service`)。
- `loki.process` 共用遮蔽:`Bearer\s+[^\s"',;]+`(遇到引號、逗號、分號即停,避免吃掉 JSON 的結尾引號;Task 3.1 code-review 前發現 `Bearer\s+\S+` 會把 `"Bearer abc"` 變成 `"[REDACTED]`,opsx:update 使用者確認)、JWT(`eyJ[\w-]+\.[\w-]+\.[\w-]+`)、email → `[REDACTED]`;IP 保留。
- 遮蔽規則只寫在 `redact.alloy`(`declare` 模組),`config.alloy` 以 `import.file` 引用,測試也 import 同一份檔案,確保測到的就是正式規則。
- 遮蔽規則以樣本 log 驗證:用 docker 跑 Alloy,`loki.source.file` 讀 fixture,`loki.echo`(或寫檔)輸出後比對。在 Task 3 實作時確定驗證方式,但驗證本身不可省略。
- 不改 log 格式(JSON 化另開 change)。

### D8. 資源、權限與持久化(grill Q3/Q11/Q13)

- Alloy `mem_limit: 200m`、環境變數 `GOMEMLIMIT=150MiB`(讓 Go GC 在接近上限前積極回收)、`restart: unless-stopped`,不出現在任何 service 的 `depends_on`;UI port 12345 不 publish。
- image 釘版本 `grafana/alloy:v1.20.1`(Task 1.1 選定:2026-09-28 發布的最新 stable release;本機以 docker 驗證 `--version` 與 `fmt` 正常,multi-arch image 含 amd64)。
- 掛載全部 `:ro`:`/var/run/docker.sock`、`/run/containerd/containerd.sock`、`/sys`→`/sys`、`/`→`/rootfs`、`/proc`→`/host/proc`、`/var/lib/docker`、`/dev/disk`、`/var/log/nginx`。unix exporter 使用 `procfs_path=/host/proc`、`sysfs_path=/sys`、`rootfs_path=/rootfs`。以 root 執行(讀取 `root:adm 640` 的 nginx log)。**不開 `privileged`、不加 capabilities、不用 `pid: host`。**
- containerd.sock 與 `/sys` 原路徑(Task 3.1 實測後 opsx:update,使用者確認):Alloy v1.20.1 的 cadvisor 讀 Docker container 需經 containerd socket,只有 docker.sock 時 container metrics 為空(本機與官方文件皆證實;官方文件建議 privileged)。2026-10-01 在 EC2 實測:`containerd.sock:ro` + `/sys:/sys:ro`、不開 privileged,可取得 app/db/worker/pgbouncer/redis 各 container 的 `container_memory_working_set_bytes`(唯一的 log 是 crio factory 註冊失敗,level=info、無害)。containerd.sock 與 docker.sock 同為 root 等級權限,風險等級不變。替代:`privileged: true`——container 被入侵即可直接接管主機,不採用;拿掉 cadvisor——看不到各 container 的資源與重啟,不採用;另跑官方 cadvisor container——同樣建議 privileged 且多吃記憶體,不採用。
- 記憶體實測與調整:同次 EC2 實測只開 cadvisor(預設設定)即用 170.5MiB / 200MiB,因此加上 `GOMEMLIMIT` 與 D6 的 cadvisor 參數。不擴展 instance(使用者決定先試 t2.micro);擴展條件(Task 3.2 實測後修訂,使用者確認):`vmstat` 的 `si`/`so` 持續不為 0(swap 被頻繁讀寫),或 Alloy 反覆被 OOM kill 重啟,才另開 change 垂直擴展;單看 swap 用量會誤判——3.2 部署後 swap 用到 415MB,但 `si`/`so` 為 0、`wa` 為 0,只是閒置分頁被移入 swap。另開 change 垂直擴展時(注意 `ec2.tf` 依 AZ 選 subnet,改 instance type 可能導致 instance 被重建、資料遺失)。
- named volume `alloy_data:/var/lib/alloy/data`,以 `--storage.path` 指向,保存 positions 與 remote_write WAL,讓重啟後不重送、不遺漏。
- EC2 加 1GB swapfile、`vm.swappiness=10`、寫入 `/etc/fstab`(人工 SSM)。

### D9. 故障隔離(grill Q6)

- `deploy.sh`:任一 `GRAFANA_CLOUD_*` 或 `METRICS_TOKEN` 缺少 → 印出 `WARNING` 後繼續部署,不 `exit`(與 `DATABASE_URL_DIRECT` 缺少時中止部署的處理刻意不同)。
- `restart alloy` 失敗只警告,不影響部署結果。
- Grafana Cloud 無法連線:Alloy 自行重試並記錄 error log,WAL 有大小上限。

### D10. Dashboard 與告警(grill Q14/Q15)

- 在 Grafana Cloud UI 建立 App、Containers、Host、Logs 四張 dashboard(可匯入社群 dashboard 後調整),JSON 匯出到 `infra/grafana/dashboards/`。
- 4 條告警,通知寄到開發者 email,規則匯出到 `infra/grafana/alerts/`:
  1. 10 分鐘內收不到任何 metrics(`absent`/no data)
  2. `node_memory_MemAvailable_bytes` < 100MB 或 swap 用量 > 200MB,持續 5 分鐘
  3. 5 分鐘內 5xx 比例 > 5%
  4. 15 分鐘內任一 container 重啟
- 門檻值先用以上保守值,上線一兩週後依實際數據調整。
- 替代:Terraform grafana provider——需要另一組寫入 token,且 state 目前放在本機;留待之後做 CD 時再評估。

## Risks / Trade-offs

- [docker.sock 與 containerd.sock 等同 host root] → 所有掛載 `:ro`、不開 privileged、image 釘版本、不 publish port;單人專案接受此風險。之後有多人協作或主機上有更敏感的服務時,再評估 docker-socket-proxy。
- [記憶體不足造成 OOM] → swap 當緩衝 + Alloy `mem_limit` + 記憶體與 swap 告警;若 `si`/`so` 持續不為 0 或 Alloy 反覆 OOM 重啟,另開 change 升級 instance(見 D8)。
- [django-prometheus middleware 在每個 request 的路徑上] → 只做計數、成本極低;出問題時回滾 image。
- [multiprocess 目錄殘留] → 放 tmpfs + `on_starting` 清空 + `child_exit` mark dead。
- [series 數超過額度] → 白名單 + 部署後人工確認。
- [遮蔽規則漏網] → 樣本 log 測試;IP 刻意保留;log 在第三方保存 14 天。
- [nginx 的 404 是人工設定] → app 端 token 才是主要防線,有測試保護。

## Migration Plan

1. Task 1:Grafana Cloud stack 與 token、EC2 `.env`、swap、記錄 baseline。
2. Task 2:部署帶有 django-prometheus 的 image;設定 nginx `/metrics` 404。
3. Task 3:部署 Alloy;確認 Grafana Cloud 上有資料、series 數與 Alloy 記憶體用量。
4. Task 4:建立 dashboard 與告警。

**Rollback:**
- Alloy:`docker compose stop alloy`,或從 compose 移除後重新部署;app 不受影響。
- App(middleware / `gunicorn.conf.py`):在 EC2 上把 compose 的 app image 暫時改成上一版的 `sha-xxxxxxx`,執行 `up -d`,之後再 `git revert`。
- swap:`swapoff /swapfile`,並移除 `/etc/fstab` 中對應的那一行。
