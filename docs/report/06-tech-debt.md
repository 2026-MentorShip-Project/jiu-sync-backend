# 6. 技術債處理 Log

本章記錄開發過程中刻意做的取捨、暫時的權宜做法,以及**之後繼續開發時必須修改的地方**。每一項都寫明:

- **現況**:目前是怎麼做的。
- **為什麼**:當時的背景與取捨理由。
- **影響**:不處理會發生什麼事。
- **之後怎麼改**:建議的修正方向。

最後一節是已經處理掉的技術債紀錄,來源是 `docs/agents/incident-log/`。

## 總覽(依優先順序)

| 優先 | 項目 | 類型 |
| --- | --- | --- |
| P0 | [6.1 資料庫沒有備份](#61-資料庫沒有備份) | 資料安全 |
| P0 | [6.2 手機末三碼可被暴力嘗試、限流 IP 可被偽造](#62-公開端點的防濫用不足) | 安全 |
| P1 | [6.3 單一 EC2 包全部服務,只能垂直擴展](#63-單一-ec2-包全部服務只能垂直擴展) | 架構取捨 |
| P1 | [6.4 垂直擴展有「重建機器、資料遺失」的陷阱](#64-垂直擴展的-subnet-陷阱) | 架構取捨 |
| P1 | [6.5 部署是半自動的,且先上線新版才 migrate](#65-部署流程半自動且先上線才-migrate) | 交付流程 |
| P1 | [6.6 告警尚未建立](#66-告警尚未建立) | 可觀測性 |
| P2 | [6.7 CI 不是強制關卡、`main` 分支沒有使用](#67-ci-不是強制關卡main-分支沒有使用) | 交付流程 |
| P2 | [6.8 AI 推薦同步佔用 web 執行緒](#68-ai-推薦同步佔用-web-執行緒) | 擴展 |
| P2 | [6.9 通知信沒有重試與逾時](#69-通知信沒有重試與逾時) | 容錯 |
| P2 | [6.10 沒有網域,用 sslip.io](#610-沒有網域用-sslipio) | 架構取捨 |
| P2 | [6.11 主機上的手動設定:nginx、`.env`、Terraform state](#611-主機上的手動設定) | 維運 |
| P3 | [6.12 沒有負載測試](#612-沒有負載測試) | 擴展 |
| P3 | [6.13 可觀測性的範圍限制](#613-可觀測性的範圍限制) | 可觀測性 |
| P3 | [6.14 其他小型缺口](#614-其他小型缺口) | 混合 |
| P1 | [6.15.1 前端沒有錯誤處理頁面](#6151-沒有錯誤處理頁面) | 前端 |
| P1 | [6.15.2 前端沒有錯誤監控(Sentry)](#6152-沒有錯誤監控sentry) | 前端 |
| P2 | [6.15.3 前端沒有 E2E 與 design system 單元測試](#6153-沒有-e2e-測試與-design-system-單元測試) | 前端 |

---

## 6.1 資料庫沒有備份

- **現況。** PostgreSQL 跑在 EC2 上的 container 裡,資料存在 Docker named volume `postgres_data`,實際位置是 EC2 的根磁碟(EBS)。目前沒有 `pg_dump` 排程,也沒有 EBS snapshot。
- **為什麼。** 部署的第一個目標是讓前端有穩定的 HTTPS 端點做整合測試,取代臨時的 ngrok;當時還沒有真實使用者資料,備份沒有列入範圍。
- **影響。** EC2 故障、EBS 損毀、誤執行 `terraform destroy`,或 Terraform 判定需要重建 instance(見 6.4),資料就**全部遺失且無法復原**。停機還可以接受,資料遺失不行。
- **之後怎麼改。** 二選一,成本都很低:
  - 用 Terraform 加一條 DLM(Data Lifecycle Manager)規則,每天自動做 EBS snapshot,保留 7 份。
  - 或在 EC2 上用 systemd timer 定期 `pg_dump`,上傳 S3,並設定 lifecycle 自動刪除舊檔。

  不論哪一種,都要**實際演練一次還原**,證明備份真的能用。搬到 RDS(6.3)之後,改用 RDS 的自動備份與 point-in-time recovery。

## 6.2 公開端點的防濫用不足

- **現況。**
  - 改票前的身分核對(`POST /api/events/{id}/responses/verify`)只靠暱稱 + 手機末三碼,而末三碼只有 1,000 種組合,端點沒有任何嘗試次數限制。
  - 留言限流以 `X-Forwarded-For` 的**第一個值**當來源 IP(`apps/events/views.py` 的 `_get_client_ip()`),但這個值可以由使用者自己指定。
  - Django `/admin/` 經 nginx 對外公開,沒有登入失敗限制。
- **為什麼。** 參與者免登入是產品核心(降低填答門檻);限流只做了留言防洗版這個最明顯的需求,`X-Forwarded-For` 的取值方式是在不確定 nginx 設定的情況下寫的(nginx 設定當時不在這個 repo,見 `add-comment-rate-limit` tasks 3.3)。
- **影響。** 知道某人暱稱的人(暱稱在活動頁上是公開的),最多試 1,000 次就能改掉他的投票;每次嘗試都要做一次較慢的密碼雜湊,大量嘗試也會吃掉 t2.micro 的 CPU。留言限流可以被輕易繞過。
- **之後怎麼改。**
  - 身分核對加上失敗次數限制:依「活動 + 暱稱」與來源 IP 計數,例如失敗 5 次鎖 15 分鐘,沿用 Redis 鎖的寫法。
  - 來源 IP 改讀 nginx 設定的 `X-Real-IP`(或 `X-Forwarded-For` 的最後一個值)。
  - nginx 對外封鎖 `/admin/`,需要時透過 SSM port forwarding 使用。

## 6.3 單一 EC2 包全部服務,只能垂直擴展

- **現況。** 一台 t2.micro(1 vCPU、約 1GB 記憶體)用 docker compose 同時跑 app、worker、Postgres、PgBouncer、Redis、Alloy;nginx 直接裝在主機上。前面沒有負載平衡器。
- **為什麼。** 這是 MVP 階段刻意的取捨:
  - **成本。** t2.micro 與 20GB gp3 都在 AWS free tier 內,只額外付 Elastic IP 的費用(約 USD 0.005/小時)。改用 ALB、RDS、ElastiCache、ECS/Fargate,每月固定費用會多出數十美元,在目前接近零的流量下換不到等值的好處。
  - **用途。** 目前主要提供前端整合測試與少量真實使用,一台機器足以負荷。
  - **單人維運。** 只有一個環境、一台機器,出問題時排查範圍小。
  - **容器化讓之後的遷移成本變低。** image 放在 GHCR、設定全走環境變數、app 不保存狀態、有 `/healthz/`。搬到 ECS 時主要改的是基礎設施,程式碼幾乎不用動。
- **影響。**
  - **只能垂直擴展。** 資料庫與 Redis 都在這台機器上,不能直接多開幾台 EC2 分流,否則每台會有各自的資料庫,資料不一致。
  - **單點故障。** 機器掛掉,整個服務就停了;部署時也會有短暫中斷。
  - **資源互搶。** 所有服務共用約 1GB 記憶體,任一個用量暴增都會影響其他服務(目前靠 1GB swap 與 Alloy 的 `mem_limit` 緩衝)。
- **之後怎麼改。** 依第 3 章 3.5 的路線圖分階段進行:
  1. 先把狀態拆出去:Postgres 改 RDS、Redis 改 ElastiCache,並購買網域。
  2. 前面接 ALB(搭配 ACM 憑證),app 放進 ECS(EC2 launch type 或 Fargate)或 ASG,依負載增減數量。
  3. worker 獨立成另一個 service,依佇列長度擴充。
  4. 配套:`.env` 改用 SSM Parameter Store 或 Secrets Manager 注入;migrate 改成部署前以一次性 task 執行;多個 app 連同一個 RDS 時,用 PgBouncer sidecar 或 RDS Proxy 控制總連線數。

  **觸發條件**(碰到任一項才進行,不提前付成本):開始有真實使用者、停機會造成實際損失;垂直擴展後資源仍持續不足;需要零停機部署;協作者變多、需要 staging 環境。

## 6.4 垂直擴展的 subnet 陷阱

- **現況。** `infra/terraform/ec2.tf` 先查出「提供 t2.micro 的可用區域」,再從這些區域的 subnet 中取第一個來放 instance(`element(data.aws_subnets.instance_az.ids, 0)`)。目前只有 `ap-northeast-3a` 符合。
- **為什麼。** 建立 instance 時,必須避開不提供 t2.micro 的可用區域,這是最直接的寫法。
- **影響。** 如果直接把 `instance_type` 改成 t3.small,符合條件的可用區域會變多,取到的「第一個 subnet」可能改變,Terraform 就會判定 instance 必須**重建**。資料庫資料(6.1)、`.env`、nginx 設定、TLS 憑證、swap 都在這台機器上,會一起消失。
- **之後怎麼改。** 需要垂直擴展時(觸發條件:`vmstat` 的 `si`/`so` 持續不為 0,或 Alloy 反覆被 OOM kill):
  - 先完成 6.1 的備份,並手動做一次 EBS snapshot。
  - 把 subnet 固定成目前使用的那一個,只改 `instance_type`。
  - `terraform plan` 必須顯示 **update in-place**,不可以出現 `must be replaced`。
  - t3 系列記得設定 `credit_specification` 為 `standard`,避免 CPU credit 超用而額外收費。

## 6.5 部署流程半自動,且先上線才 migrate

- **現況。**
  - PR 合併進 `develop` 後自動建置 image 並推到 GHCR,但**部署要由開發者在本機手動執行** `infra/scripts/deploy.sh`。
  - 正式環境的 compose 使用 `latest` tag。
  - `deploy.sh` 的順序是 `pull` → `up -d` → `migrate`:新版程式碼先開始接請求,資料表才更新。
  - 沒有自動化的 rollback;要回滾時,手動把 compose 的 image 改成上一版的 `sha-xxxxxxx`。
- **為什麼。** 「image 推上去後自動觸發部署」被刻意排除在 `deploy-django-app` 的範圍外,留給之後的 change。原本的設計是在 EC2 上 git clone,但卡在 GitHub fine-grained PAT 需要組織管理員核准,才改成「CI 建置 + EC2 從 GHCR 拉 image」。
- **影響。**
  - 合併後到上線之間依賴人工,容易忘記部署,或部署到不是預期的版本(`latest` 只代表「最近一次建置」)。
  - 新增欄位、改欄位名稱這類 migration,在 `up -d` 到 `migrate` 完成之間,新程式碼可能查詢不存在的欄位而回 500。
- **之後怎麼改。**
  - 補上 CD:`build-push` 成功後自動觸發部署,並以 `sha-xxxxxxx` 指定版本,不依賴 `latest`。
  - 調整部署順序:先用新 image 跑一次性的 `migrate`,再 `up -d`;migration 採「先擴充、後收斂」(expand-then-contract)寫法,讓新舊兩版程式碼都能在過渡期間正常運作。
  - 部署後自動打 `/healthz/` 與幾個關鍵 API,失敗時自動切回上一個 sha。

## 6.6 告警尚未建立

- **現況。** Grafana Cloud 上已經有 App、Containers、Host、Logs 四張 dashboard,但告警還沒建立(`add-observability-stack` task 4.2)。
- **為什麼。** 告警需要在 Grafana Cloud UI 手動建立並設定 email 通知,排在 dashboard 之後進行。
- **影響。** 服務出問題時不會主動通知,要有人剛好去看 dashboard 才會發現。
- **之後怎麼改。** 依 design D10 建立 4 條告警並匯出到 `infra/grafana/alerts/`:10 分鐘內收不到 metrics、可用記憶體 < 100MB 持續 5 分鐘、5 分鐘內 5xx 比例 > 5%、15 分鐘內任一 container 重啟。上線一兩週後依實際數據調整門檻。

## 6.7 CI 不是強制關卡、`main` 分支沒有使用

- **現況。**
  - GitHub 組織是免費方案、repo 是 private,無法設定 branch protection。CI 失敗的 PR 技術上仍然可以合併(`add-ci-pipeline` task 6.1 尚未完成)。
  - 所有開發都合併進 `develop`,正式環境也是用 `develop` 建置的 image;`main` 目前只有初始 commit。
- **為什麼。** 團隊規範是「禁止直接 push 到 `main`/`develop`,一律走 PR」,目前靠自律遵守。
- **影響。** CI 只是參考,不是保證;`main` 沒有代表「正式環境版本」的意義,README 的分支規範和實際做法不一致。
- **之後怎麼改。** 組織升級方案後,為 `main`、`develop` 設定 required checks(`lint`、`test`、`test-pgbouncer`、`spec-sync`、`integration`)。同時決定:正式環境改從 `main` 建置,或修改 README 的分支規範以符合現況。

## 6.8 AI 推薦同步佔用 web 執行緒

- **現況。** `POST .../restaurant-recommendations/` 在 web 請求內同步呼叫 Perplexity,最長約 45 秒(連線 5 秒 + 整體期限)。gunicorn 總共只有 12 個執行緒可以同時處理請求。
- **為什麼。** 同步的寫法最簡單,前端也只需要等一個回應;AI 推薦的使用頻率低,而且有每月額度與「同活動同時只能一個請求」限制。
- **已知的細節限制。**
  - gunicorn 在 `gthread` 模式下的 `timeout=60` 不會中斷跑太久的單一請求。
  - 引擎的整體期限只在讀到每一段資料之間檢查,單次讀取卡住時仍可能超過期限。經討論後決定不再修改引擎(`add-ai-restaurant-recommendation` task 5.3、design D5):期限只當成使用體驗上限,**額度正確性改由「確認時在鎖內再檢查一次」保證**(task 5.4),所以就算請求超時,額度也不會被超用。
- **影響。** 多個主揪同時使用 AI 推薦時,會佔掉一部分執行緒,其他 API 可能需要排隊。
- **之後怎麼改。** 當 Grafana 顯示 AI 請求開始影響其他 API 的延遲時,改成非同步:API 建立任務後立即回 202 與任務 id,由 worker 呼叫上游,前端輪詢結果。現有的 pending → succeeded/failed 狀態設計可以直接沿用。

## 6.9 通知信沒有重試與逾時

- **現況。** Celery task 沒有設定自動重試;SMTP 沒有設定逾時(`EMAIL_TIMEOUT` 未設定,Django 預設為無逾時);worker `--concurrency=1`。
- **為什麼。** 通知信是附加功能,第一版優先確保「寄信失敗不影響 API」,重試留待之後。`concurrency=1` 是為了 t2.micro 的記憶體。
- **影響。** 寄信失敗就不會再寄;SMTP 伺服器卡住時,worker 會停在那封信,後面所有的信都要等。
- **之後怎麼改。** 設定 `EMAIL_TIMEOUT`(例如 10 秒);task 加上有上限的重試(例如最多 3 次、指數退避)。task 內已經有「過期 task 不寄信」的判斷,重試不會造成重複寄送。

## 6.10 沒有網域,用 sslip.io

- **現況。** 對外網址是 `56-155-65-244.sslip.io`,透過免費的萬用 DNS 服務把網域對應到 Elastic IP,再向 Let's Encrypt 申請憑證。
- **為什麼。** 不用購買網域就能有瀏覽器信任的 HTTPS。
- **影響。** 網址依賴第三方 DNS 服務;換 IP 就要換網址,前端與 CORS 設定都要跟著改;無法使用 ACM 憑證,所以也不能接 ALB(6.3);HSTS 的 preload 設定實際上無法生效。
- **之後怎麼改。** 購買正式網域,和 6.3 的 ALB + ACM 一起進行。

## 6.11 主機上的手動設定

- **現況。**
  - **nginx** 直接裝在 EC2 上,repo 中的 `infra/nginx/` 只是參考副本,部署腳本不會同步它。
  - **`.env`** 透過 SSM session 手動建立在 `/opt/jiu-sync-backend/.env`。
  - **swap** 與 `vm.swappiness` 是手動設定的。
  - **Terraform state** 存在開發者本機,`terraform apply` 也在本機執行。
- **為什麼。** 單人、單一環境時,手動設定最快;把這些設定自動化需要額外的工具與權限設計。
- **影響。** 機器重建時,這些設定都要依文件手動還原(例如 `/metrics` 的 nginx 封鎖規則,曾在部署驗證時被誤還原);Terraform state 只有一份,本機遺失就無法再用 Terraform 管理現有資源;多人協作時無法共用。
- **之後怎麼改。** Terraform state 改放 S3(搭配鎖定機制);機密改用 SSM Parameter Store 或 Secrets Manager;nginx 與 swap 的設定寫進 user-data 或改由 ALB 取代 nginx(6.3)。

## 6.12 沒有負載測試

- **現況。** CI 只有一個 k6 smoke test(1 次請求打 `/healthz/`),沒有任何負載或壓力測試數據。
- **為什麼。** 優先完成功能與正確性;目前流量很小。
- **影響。** 不知道目前的架構實際能承受多少流量,也無法用數據判斷 6.3 的擴展時機。
- **之後怎麼改。** 見第 3 章 3.3:寫一個模擬真實流量組合的 k6 容量測試,在與正式環境相同規格的環境執行,並以 Grafana 觀察瓶頸。

## 6.13 可觀測性的範圍限制

- **現況。**
  - 只有 `apps.recommendations` 輸出結構化 JSON log,其他 app 是 Django 預設的純文字。
  - 沒有收集 Postgres、Redis、PgBouncer、Celery 的指標,也沒有資料庫查詢次數。
  - log 在 Grafana Cloud 只保留 14 天(free tier)。
- **為什麼。** 把全站 log 改成 JSON 會改變所有 app 的輸出格式,刻意另開 change 處理;換成會記錄查詢次數的資料庫 backend,會讓觀測程式碼進入每一條 SQL 的路徑,可能影響 PgBouncer 相容性,違反「觀測不影響 app」的原則。
- **影響。** 查非 AI 功能的問題時,只能用文字搜尋 log;資料庫變慢時,只能從 view 的延遲間接推測。
- **之後怎麼改。** 全站 log 改為 JSON(沿用 `apps.recommendations` 的 `JsonFormatter`);需要時再加 Postgres exporter。

## 6.14 其他小型缺口

| 項目 | 現況與影響 | 之後怎麼改 |
| --- | --- | --- |
| refresh token 重複使用偵測 | 已撤銷的 token 被重用時只回 401,不會撤銷同使用者的其他 token;同一 token 幾乎同時換發兩次,兩次都可能成功 | 換發時鎖住紀錄;偵測到重用時撤銷該使用者的全部 refresh token |
| access token 無法提前撤銷 | 登出後,手上的 access token 最多還能用 30 分鐘 | 目前接受;需要時縮短效期或加入撤銷清單 |
| 編輯活動沒有條件式寫入 | `PATCH /api/events/{id}` 先檢查狀態再儲存,與定案同時發生時,可能改到剛定案的活動 | 改用條件式 UPDATE 或在交易內鎖住活動列 |
| 建立活動與留言不是冪等的 | 網路斷線後使用者重送,可能重複建立 | 需要時加入 Idempotency-Key header |
| Google 登入每次重新下載憑證 | 預設逾時 120 秒,Google 很慢時會佔住執行緒 | 快取 Google 憑證,並縮短逾時 |
| `django-prometheus` 使用預發布版 | 釘在 `2.6.0.dev22`,因為正式版還不支援 Django 6.1 | 正式版支援 Django 6.1 後改用正式版 |
| 加固項目 | 未強制 IMDSv2、EBS 加密未明確設定、沒有依賴弱點掃描 | 在 Terraform 加上 `metadata_options` 與 `encrypted = true`;CI 加 `pip-audit` 與 Dependabot |
| 工作目錄的重複檔 | 有一批 `xxx 2.py`、`xxx 2.md` 未追蹤檔案,看起來是 Finder 或同步工具複製出來的 | 確認內容與原檔相同後刪除 |

---

## 6.15 前端技術債

以下三項屬於前端 repo(`that-is-so-sweet`,React 19 + Vite,部署在 Vercel),但會直接影響使用者體驗與後端問題的排查,一併記錄在這裡。

### 6.15.1 沒有錯誤處理頁面

- **現況。** 前端沒有 React Error Boundary,也沒有 404 或通用錯誤頁。自訂路由(`src/share/routing/router.ts`)遇到無法辨識的路徑時,一律導回首頁;元件在渲染時拋出例外,整個畫面會變成空白。API 的錯誤目前由各頁面自行處理,散落在十多個 `catch` 裡。
- **為什麼。** 開發初期優先完成主揪與參與者的主要流程,異常情境的畫面還沒有統一設計。
- **影響。**
  - 渲染錯誤會讓使用者看到一片空白,不知道發生什麼事,也沒有「重新整理」或「回首頁」的出口。
  - 打錯活動網址會默默回到首頁,使用者以為連結還有效。
  - 後端已經回傳明確的錯誤(例如 410 `LINK_EXPIRED`、409 `VOTING_CLOSED`),但沒有統一的頁面能好好呈現。
- **之後怎麼改。**
  - 在 App 最外層加上 Error Boundary,顯示錯誤頁並提供「重新整理 / 回首頁」。
  - 新增 404 頁(活動不存在、路徑錯誤)與「連結已失效」頁,對應後端的 404 `EVENT_NOT_FOUND` 與 410 `LINK_EXPIRED`。
  - 在 `src/api/http.ts` 統一處理錯誤:依後端回傳的 `code` 決定要顯示 toast、表單欄位錯誤,還是導到錯誤頁。

### 6.15.2 沒有錯誤監控(Sentry)

- **現況。** 前端沒有任何錯誤回報機制。後端已經有 Grafana(metrics + log),但瀏覽器端發生的錯誤,開發者完全看不到。
- **為什麼。** 先建立後端的可觀測性(`add-observability-stack`);前端監控還沒排進範圍。
- **影響。**
  - 使用者遇到 JavaScript 錯誤、白畫面或 API 呼叫失敗,除非主動回報,開發者不會知道。
  - 無法把前端錯誤和後端 log 串在一起追查,例如「這個 500 是哪個畫面、哪個操作觸發的」。
  - 搭配 6.15.1(沒有錯誤頁),目前前端的故障對使用者和開發者都是不可見的。
- **之後怎麼改。**
  - 導入 Sentry(免費方案即可),在 Error Boundary 與 `http.ts` 的錯誤路徑回報,並上傳 source map,讓錯誤能對應到原始程式碼行數。
  - 回報前遮蔽個資:不送出手機末三碼、email、access token,比照後端 Alloy 的遮蔽規則。
  - 之後可以在 API 請求帶上 request id,讓 Sentry 的事件和後端 log 互相對應。

### 6.15.3 沒有 E2E 測試與 design system 單元測試

- **現況。**
  - 前端有 10 個 Vitest 單元測試,全部集中在 `src/lib`(路由、API client、輪詢比對、表單驗證等邏輯)。
  - `src/design-system/components` 的 6 個共用元件(Avatar、Badge、Button、Input、ProgressBar、Tag)沒有任何測試。
  - 沒有 E2E 測試(沒有 Playwright 或 Cypress)。
  - 前端的 GitHub Actions(`deploy-pages.yml`)只在 `main` 上執行 build 與部署,不跑 `vitest`,也不跑型別檢查。
- **為什麼。** 單元測試優先放在邏輯最複雜、最容易出錯的地方;UI 元件與完整流程的測試還沒有建立。
- **影響。**
  - 共用元件被修改時,沒有測試能發現其他畫面跟著壞掉。
  - 主要流程(建立活動 → 分享 → 投票 → 改票 → 定案)只能靠手動點擊驗證,前後端串接的問題(例如 API 欄位改名)要到正式環境才會被發現。
  - 既有的單元測試不在 CI 中執行,測試失敗也不會擋住部署。
- **之後怎麼改。**
  - CI 先加上 `tsc --noEmit` 與 `vitest run`,並在 PR 階段執行,不只在 `main` 上建置。
  - design system 元件用 Vitest + React Testing Library 測試互動、狀態與無障礙屬性(例如 Button 的 disabled、Input 的錯誤狀態)。
  - 用 Playwright 寫 1–2 條關鍵路徑的 E2E 測試:主揪建立活動,以及參與者投票再改票。可以先打後端的本機 docker compose 環境,登入部分用測試用的 token 取代 Google 登入。

---

## 6.16 已處理的技術債紀錄

以下是開發過程中發現並已修正的問題。完整紀錄見 `docs/agents/incident-log/`。

| 日期 | 問題 | 怎麼發現 | 怎麼處理 |
| --- | --- | --- | --- |
| 2026-09-21 | 改票憑證有 TOCTOU 漏洞:兩個請求同時用同一組憑證,都會成功 | Codex 二次審查 | 改成條件式 UPDATE 消費憑證,並在同一個 UPDATE 內重新檢查是否過期 |
| 2026-09-23 | 寄通知信失敗,會讓已經成功的定案變成 500 | 使用者實測 | 寄信改為 `on_commit` 排入並捕捉例外,只記 log |
| 2026-09-23 | 重新開放與取消,被「連結已失效」檢查誤擋,定案超過 7 天的活動無法重新開放 | code-review | 主揪的管理動作不再套用連結失效檢查 |
| 2026-09-26 | `apps/notifications/tests.py` 檔名不符合 pytest 收集規則,9 則測試從來沒跑過 | 撰寫新測試時發現 | 改成 `tests/` package,確認全部被收集且通過 |
| 2026-09-26 | Redis URL 設定錯誤時拋 `ValueError` 而不是 `RedisError`,留言全面 500,違反 fail-open | code-review 實測 | 一併捕捉 `ValueError`,並重用 Redis client |
| 2026-09-28 | 結構化 log 遇到 `NaN` 會輸出不合法的 JSON,Loki 無法解析 | code-review | 改輸出 `null`;序列化失敗時改輸出最小的合法 JSON |
| 2026-09-28 | AI 推薦成功但寫入失敗時,紀錄停在 pending,佔用額度 5 分鐘 | code-review | 成功寫入包在 savepoint 內,失敗時轉為 failed |
| 2026-09-28 | 上游連線錯誤沒有被分類、model 回退時沒有可觀測的訊號 | code-review | 新增 `UpstreamConnectionError`;log 加上 `model_fallback` 與 `record_missing` 欄位 |
| 2026-09-29 | 確認額度時的鎖順序與「刪除使用者」相反,會互相等待造成 deadlock | code-review,以測試重現 | 改為先鎖紀錄、再鎖使用者 |
| 2026-09-29 | 上游回應含 NUL 字元時,Postgres 拒收,紀錄殘留 pending,上游文字進入 log | code-review | 寫入前移除 NUL,例外訊息不夾帶上游內容 |
| 2026-09-29 | `displayStatus` 用 UTC 日期判斷聚會是否已過,台灣時間 00:00–08:00 會算錯 | 測試在特定時段失敗 | 改以台灣時間的當天判斷(`fix-display-status-timezone`) |
| 2026-09-29 | 正式環境沒有 Celery worker,broker URL 也指錯,通知信從來沒寄出過 | 盤點正式環境時發現 | 新增 worker service、修正 broker URL(`add-pgbouncer-and-celery-worker`) |
| 2026-09-29 | 本機 Terraform state 會被打包進 Docker build context | code-review | 新增 `.dockerignore` 排除 `.env` 與 state |
| 2026-09-29 | app 與 worker 的 `DJANGO_SETTINGS_MODULE` 來源不一致,可能載入不同設定 | 實作時發現 | 兩者都在 compose 明確指定 prod settings |
| — | `Event.mode` 缺少 migration,CI 的 `makemigrations --check` 失敗 | CI | 補上 migration(`fix-missing-mode-migration`) |
| 2026-10-01 | 部署驗證時,nginx 的 `/metrics` 封鎖規則被誤還原 | 部署驗證 | 重新加回規則,並把 nginx 設定的參考副本放進 `infra/nginx/` |
