# 3. 擴展設計

本章說明揪甘心預期的使用規模、目前為效能與擴展做的設計、效能測試的現況,以及流量成長時的擴展路線。目前架構「只能垂直擴展」這個取捨的背景與代價,記錄在第 6 章。

## 3.1 預期規模與流量特徵

- **一場活動的人數小。** 一場揪團通常是幾個人到幾十人,候選時段最多 20 個。單一活動的資料量很小,查詢成本主要來自請求次數,而不是資料量。
- **流量是一波一波的。** 主揪把連結丟進群組後,大家會在短時間內集中點開、投票;定案時又會有一波人回來看結果。
- **輪詢是固定的背景流量。** 開著活動頁的每個人,每 10 秒會呼叫一次 `GET /api/events/{id}/poll`。也就是說,**同時開著頁面的人數 ÷ 10 ≈ 輪詢的 RPS**。例如 100 人同時開著頁面,光輪詢就有約 10 RPS。
- **AI 推薦很慢但很少。** 只有主揪、只有活動定案後、每人每月有次數上限,所以次數少;但每次呼叫上游要等十幾秒以上(開發時實測中位數約 24 秒,上限 45 秒),會長時間佔住一個處理請求的執行緒。

## 3.2 目前為效能與擴展做的設計

### 請求處理容量

- **gunicorn `gthread`:3 workers × 4 threads。** 最多可以同時處理 12 個請求。選 thread 而不是單純多開 process,是因為 t2.micro 只有約 1GB 記憶體,而且 AI 推薦大部分時間在等網路,用 thread 等待比多開 process 省記憶體(`gunicorn.conf.py`)。
- **耗時工作移出請求。** 寄信交給 Celery worker,API 不需要等 SMTP 回應。

### 資料庫連線

- **PgBouncer transaction pooling。** app 的 12 個執行緒加上 worker,透過 PgBouncer 共用最多 10 條 Postgres 連線(`DEFAULT_POOL_SIZE=10`,最多接受 100 個 client 連線)。連線只在 transaction 期間被佔用,所以少量的 Postgres 連線就能服務較多的執行緒。之後把 app 水平複製成多個 container 時,總連線數也不會直接打爆 Postgres。
- **重用到 PgBouncer 的連線。** Django `CONN_MAX_AGE=60`,不用每個請求重新建立連線;`CONN_HEALTH_CHECKS=True` 避免拿到已被中斷的連線。
- **CI 守住相容性。** `test-pgbouncer` job 經 PgBouncer 跑完整測試,確保程式碼沒有用到 transaction pooling 不支援的功能(例如跨 transaction 的 server-side cursor)。

### 查詢成本

- **輕量輪詢端點。** 輪詢只做 3 個查詢(活動、投票數與最新時間、留言數與最新時間),不載入投票明細(第 2 章 2.3)。
- **避免 N+1 查詢。** 回傳完整活動詳情的端點共用 `_event_with_responses_queryset()`,以 `select_related` / `prefetch_related` 一次載入時段、投票與表態。
- **留言用 keyset 分頁。** 以 `(created_at, id)` 為游標往回翻,每頁固定 10 則。不論留言有多少,每頁的查詢成本都差不多,不像 offset 分頁越往後越慢。
- **額度查詢有索引。** AI 推薦紀錄表建了 `(user, quota_period, status)` 與 `(user, event, status)` 索引,計算額度與檢查進行中請求都不用掃全表。

### 保護共用資源

- **AI 推薦:每人每月額度 + 同活動同時只能有一個請求。** 這同時控制了上游費用,以及被 AI 請求佔住的執行緒數量。
- **呼叫上游時不持有鎖。** 額度預留只在很短的 transaction 裡持有使用者列鎖,呼叫 Perplexity 的幾十秒內不鎖任何資料,不會擋住其他請求。
- **留言限流。** 同一 IP 對同一活動 2 秒只能留一則,用 Redis `SET NX EX` 完成,不增加資料庫負擔。

### 為之後的水平擴展先準備好的部分

- **app 不保存狀態。** API 以 JWT 認證,不依賴記憶體中的 session;refresh token 撤銷紀錄在 Postgres,限流鎖在 Redis。只要資料庫與 Redis 搬出這台機器,多開幾個 app container 就能分流。
- **image 與設定分離。** 同一個 image 透過環境變數切換設定,app 與 worker 共用同一個 image。
- **健康檢查端點。** `/healthz/` 不碰資料庫、只回 200,可以直接當成負載平衡器的 target health check。

## 3.3 效能測試現況

**目前沒有做過負載測試或壓力測試。** CI 的 `integration` job 只有一個 k6 smoke test(`tests/k6/healthz-smoke.js`:1 個 VU、1 次請求打 `/healthz/`),用途是確認 server 能啟動、路由正常,不代表任何容量數字。

參考報告規格提供的指標,本專案設定的目標如下(**尚未驗證**):

| 指標 | 目標 | 目前能怎麼觀測 |
| --- | --- | --- |
| 核心 API P95 延遲 | < 500 ms(AI 推薦除外) | Grafana「Jiu-Sync / App」的 `Latency p95 by view` |
| HTTP 5xx 比例 | < 1% | Grafana 的 `4xx / 5xx ratio`;nginx log 的 5xx 面板 |
| 吞吐量 | 約 100 RPS(Python 參考值) | Grafana 的 `Request rate by view` |

**建議的下一步:**

1. 寫一個 k6 容量測試,模擬真實流量組合:以輪詢與活動詳情為主的讀取、少量投票與改票、少量留言。
2. 對一個與正式環境相同規格的環境(t2.micro)執行,不要直接打正式環境,避免汙染資料與觸發 AI 費用;AI 推薦以假引擎代替。
3. 搭配 Grafana 的 App 與 Host dashboard 觀察:先碰到的是 12 個執行緒、PgBouncer 連線池,還是 1GB 記憶體。

## 3.4 已知瓶頸(依可能先碰到的順序)

| 瓶頸 | 說明 | 觀測方式 |
| --- | --- | --- |
| 記憶體 | 整台機器 952MB,要容納 app、worker、Postgres、Redis、PgBouncer、Alloy。部署 Alloy 後已加 1GB swap | Host dashboard 的 `Memory available`;`vmstat` 的 `si`/`so` |
| 被 AI 推薦佔住的執行緒 | 一個 AI 請求最長佔用一個執行緒約 45 秒以上。12 個執行緒中若有多個被 AI 佔住,其他 API 就要排隊 | App dashboard 的 `AI restaurant recommendation latency p95` |
| 單一 Celery worker | `concurrency=1`,通知信一封一封寄;某封信卡住時,後面的信都要等 | worker 的 log |
| 單機 | 任何資源用完都沒有第二台可以分流,只能升級機器規格 | — |

## 3.5 擴展路線圖

目前只能垂直擴展(升級機器規格)。要能水平擴展,必須先把資料庫與 Redis 搬出 app 所在的機器。建議依下列階段進行,**每個階段都有明確的觸發條件,沒有碰到就不提前做**,避免在流量還很少時付出不必要的成本。

| 階段 | 做什麼 | 觸發條件 |
| --- | --- | --- |
| 0. 現況 | 單台 t2.micro + docker compose | — |
| 1. 補齊安全網 | 資料庫備份(EBS snapshot 或 `pg_dump` 上傳 S3)、建立告警 | **立即**,不需要等流量成長(見第 6 章) |
| 2. 垂直擴展 | 升級為 t3.small 等較大規格 | `vmstat` 的 `si`/`so` 持續不為 0,或 Alloy 反覆被 OOM kill。注意 `ec2.tf` 的 subnet 陷阱,見第 6 章 |
| 3. 拆出狀態 | Postgres 改 RDS、Redis 改 ElastiCache;購買網域 | 開始有真實使用者、停機會造成實際損失,或垂直擴展已不划算 |
| 4. 水平擴展 | ALB(ACM 憑證)+ ECS(EC2 或 Fargate)或 ASG,app 與 worker 分成兩個 service,各自依負載調整數量 | app 的 CPU 或延遲在尖峰時持續超標,或需要零停機部署 |
| 5. AI 推薦非同步化 | 改成「送出後回 202 + 任務 id,前端輪詢結果」,上游呼叫移到 worker | AI 請求佔住 web 執行緒,開始影響其他 API 的延遲 |

拆到階段 4 時需要一起處理的事:

- **連線數。** 多個 app container 連同一個 RDS,總連線數會乘上 container 數量。可以每個 task 掛一個 PgBouncer sidecar(延續現在的設定),或改用 RDS Proxy(另外收費)。
- **migrate 只跑一次。** 不能讓每個 container 啟動時都跑,要改成部署前以一次性 task 執行。
- **Secrets 與 log 不能留在主機上。** `.env` 改用 SSM Parameter Store 或 Secrets Manager 注入;log 本來就已經由 Alloy 送到 Grafana Cloud,換平台時改用對應的收集方式即可。
- **TLS 改由 ALB 處理。** ACM 憑證需要自己擁有的網域,`sslip.io` 無法使用。
