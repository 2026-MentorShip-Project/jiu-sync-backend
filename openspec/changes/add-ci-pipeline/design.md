## Context

技術棧:Django 6.1 + DRF + SimpleJWT + django-environ + Postgres(透過 `DATABASE_URL`)+ Celery(已搭骨架但未使用)+ Redis(只有 broker URL,未使用)+ `uv` 管依賴 + `ruff` 做 lint + `pytest-django` 跑測試(`testpaths = apps, tests`,settings module 為 `config.settings.dev`)。目前沒有 `.github/workflows/`。`openspec/` + `.claude/commands/opsx/*` 是本專案 SDD 流程的骨幹,CLAUDE.md 硬性要求 CI 要擋 spec/impl 不同步(第95行)—— 詳見 proposal.md。

輸入端參考了兩份不同生態系的 CI 設計:

- **Go 範例**:3 個平行 job(`unit` 含 `-race`、`lint` 用 golangci-lint、`integration` 把 binary build 起來、跑起來、用 k6 打 E2E + load test),有 `concurrency`+`cancel-in-progress`、`permissions: contents: read`,失敗時 `always()` 上傳 log artifact。
- **Django 範例**:1 個序列 job(checkout → uv → ruff → migrate → pytest+cov → Codecov 上傳),觸發條件為 `push`+`pull_request` 到 `main`+`develop`,LINE/Gemini 等 env 用 dummy secret 帶過。

使用者確認要把 Go 範例 `integration` job 的三個要素(1. 真的把 server 建置/啟動起來再輪詢 healthcheck;2. 用 k6 打 E2E;3. 失敗 cat log + `if: always()` 關 server + 上傳 log artifact)一併搬進來,細節見下方 Decision 8-12。

## Goals / Non-Goals

**Goals:**
- 快速、平行的回饋(lint 壞掉不用等 DB job 起來)。
- 補上兩份參考都沒處理、但 Django 專案很常踩到的兩個洞:missing migration,以及(本專案特有)spec/impl 漂移。
- 把「server 真的起得來」納入 CI,不只是 `pytest-django` 那種不起真實 process 的測試(採用 Go 範例 `integration` job 的做法)。
- 預設成本最小:能不接外部服務就不接(Codecov 仍不接);k6 是使用者明確要求比照 Go 範例引入的例外,理由見 Decision 9。

**Non-Goals:**
- 不做涉及商業邏輯/JWT 登入流程的 E2E(例如打 `apps.events` 的實際業務 API)—— 那一層由既有的 `pytest-django` + `APIClient` 覆蓋,不重複。新的 `integration` job 的 E2E 範圍只到「server 起得來、路由活著」,見 Decision 11。
- 不設 coverage 百分比門檻(`--cov-fail-under`)—— 沒人要求,目前只做可視化(`--cov-report=term-missing`)。
- 不開 Redis service container —— `apps/**` 目前沒有任何程式碼在用 Celery 或 Redis-backed cache。

## Decisions

**1. `lint`、`test`、`integration` 三個平行 job,不是單一序列 job。**
採用 Go 範例的平行 job 結構,而非 Django 範例的單一序列 job:一個 trivial 的 PR 若只是 lint 紅燈,不該還要等 Postgres 開起來、整套 test 跑完才知道。哪個 job 紅了,光看 job 名稱就知道是哪個階段壞的,不用開 log。`integration` 依賴 `test` 已經驗過的 migrate 邏輯,但關注點不同(server 起得來 vs. 商業邏輯正確),所以拆成獨立 job 而不是塞進 `test` —— 見 Decision 8-12。

**2. `test` job 在 `migrate` 之前先跑 `makemigrations --check --dry-run`。**
兩份參考 CI 都沒有這條。這是 Django CI 最常見的漏洞:改了 model 欄位,忘記跑 `python manage.py makemigrations`,本機測試因為 dev DB 會自動套用最新 schema 所以照樣過,漏掉的 migration 檔案要等別人 pull `main` 之後、DB schema 跟 model 對不上才會爆出來。`--check --dry-run` 在 CI、在合併前就先失敗,代價只是多一行指令。

**3. 新增 `spec-sync` job,實作 CLAUDE.md 第95行的結構檢查。**
本專案自訂規則(兩份參考都沒有,因為它們都沒用 OpenSpec)要求 CI 擋下「動到 `apps/**` 實作卻沒同步動到 `openspec/specs/**` 或 `openspec/changes/**`」的 PR。實作方式是在獨立 job 裡跑純粹的 `git diff --name-only origin/<base>...HEAD` 路徑比對 —— 確定性、不叫 LLM,符合 CLAUDE.md 明講的「這層要停在結構檢查,不做語意判斷」(語意檢查留給人 + `spectra analyze`/`drift`,見 CLAUDE.md「PR 前的 Spec 一致性檢查」章節)。

**4. 不接 Codecov / 不上傳外部 coverage。**
Django 範例透過 `CODECOV_TOKEN` 上傳 Codecov。本專案沒接 Codecov project,也沒設對應 secret —— 現在加這個步驟只會靜默 no-op 或直接失敗。job log 裡的 `--cov-report=term-missing` 已經足夠可視化,之後真的要做趨勢追蹤,加回上傳步驟只是 4 行 diff 的事。

**5. `test` job 不開 Redis service container。**
Go 範例的 `integration` job 會起真實基礎設施(server binary + k6),因為被測程式碼真的需要。本專案 `config/celery.py` 雖然存在,但 `apps/**` 目前沒有任何 `shared_task` 落地,也沒有任何測試在 import 時真的對 `CELERY_BROKER_URL` 發連線(django-environ 只是讀字串,不會連線)。開一個沒人用的 service container 只是成本、沒有訊號 —— 等第一個 Celery task 或 Redis-backed cache 落地再補回來。

**6. 觸發條件:`pull_request` 到 `main` + `develop`,拿掉 `push`。**
Django 範例同時對同一批分支掛 `push` 跟 `pull_request`,PR merge 時會等於重跑兩次 CI(PR 上跑一次,merge 進 base branch 的 push 又跑一次)。Go 範例只掛 PR。本案保留只掛 PR,但把分支從 Go 範例的只有 `main` 拓寬成 `main`+`develop`,因為本專案實際把 `develop` 當整合分支在用,兩條線都需要同等保護。

**7. `concurrency`+`cancel-in-progress` 與 `permissions: contents: read` 照搬 Go 範例。**
這兩項是跟語言/生態系無關的最佳實踐,對本專案沒有任何壞處:PR 被 force-push 時,舊的 run 會被取消(省 CI 分鐘數);而這條 workflow 本來就不需要 push、留言或寫 package,預設 `GITHUB_TOKEN` 收斂成唯讀完全沒有副作用。

**8. `integration` job 用 `manage.py runserver` 起 server,不裝 gunicorn/daphne。**
Go 範例是把 compile 好的 binary 直接背景執行;Django 沒有等價物,而 `pyproject.toml` 目前沒有任何 WSGI/ASGI server 依賴。裝 gunicorn 是可以做的事,但屬於這次 scope 之外的新依賴決策。`manage.py runserver` 不用裝任何東西,CI 要驗的是「路由/middleware/DB 接得上」,不是效能,dev server 對這個目的已經足夠。之後真的要驗 production-like 行為(例如 worker 數、timeout 設定),再另開 change 引入 gunicorn。

**9. E2E 工具選 k6,不是 Python 生態內建的 `requests`。**
`requests` 一樣能打 `/healthz`,不用裝新 CLI、不用寫 JS。選 k6 是使用者明確要求比照 Go 範例 —— 好處是這個 CI pattern 之後要擴充成真正的 load test(像 Go 範例的 `Small load test` 步驟)時,腳本骨架已經在,不用重新選工具鏈;代價記在 Risks。

**10. 新增 `GET /healthz`,放在 `config/health.py`,不歸在任何一個 `apps/*` 底下。**
這個 endpoint 是平台層的「process 活著」訊號,不是任何一個 app(accounts/events/recommendations/notifications)的業務能力,掛在某個 app 下會造成不必要的耦合(例如日後拆微服務,health check 不該跟著 accounts app 走)。放在 `config/` 呼應 `config/urls.py`/`config/celery.py` 已經是放「跨 app 平台設定」的地方。

**11. `/healthz` 只回 200,不碰 DB。**
DB 連線是否正常,`test` job 的 `migrate` 步驟已經驗過(連不上 DB,migrate 直接失敗)。健康檢查加碼查 DB 會讓 endpoint 承擔第二種失敗模式,也讓「server process 活著」跟「DB 活著」兩件事的失敗訊號混在一起,不利於之後看 log 判斷是哪一層壞的。維持「process 有回應」這一件事就好。

**12. 失敗處理照搬 Go 範例的三個動作:失敗 `cat` server log、`if: always()` 關 server、上傳 log 當 artifact(retention 7 天)。**
背景啟動的 server 若在輪詢 healthz 階段就掛了,不印出 log 根本無從排查(CI runner 上看不到終端機輸出)。`if: always()` 確保不管 healthz 輪詢/k6 測試成功或失敗都會執行關閉 server 的步驟,避免殘留背景行程影響同一個 runner 上的後續步驟。上傳 artifact 是留一份事後可下載的完整記錄,7 天是 Go 範例的預設值,對這個專案的除錯需求足夠,也不會累積過多 storage。

## Risks / Trade-offs

- [`manage.py runserver` 不是 production-like server] → 單執行緒、非 production 建議用法,若之後 CI 要驗併發行為會不準。緩解:目前 `integration` job 只驗「起得來、路由活著」,不驗效能/併發,跟 runserver 的能力邊界一致;要驗併發時這個決策要重新評估(見 Decision 8)。
- [引入 k6 這個新 CLI 依賴,但目前只打一個 `/healthz`,用量遠低於它的能力] → 已知的權衡(見 Decision 9),換取之後擴充 load test 不用重選工具鏈。若後續一直沒有真的用上 k6 的進階能力,可以回頭評估換成 `requests`。
- [`/healthz` 只驗 process 活著,不驗 DB] → 若 DB 連線在 migrate 之後才斷(例如連線池耗盡),`/healthz` 不會抓到。可接受,因為這條 gate 的目的是「server 起得來」,不是持續健康監控(見 Decision 11)。
- [`spec-sync` job 是很鈍的路徑 diff 比對] → 可能誤傷(例如純粹修個 `apps/**` 底下的 typo,本來就不需要動 spec)或漏抓(`openspec/changes/**` 被動到,但其實跟這次改動無關、只是順手做的雜事)。緩解方式:CLAUDE.md 本身就把這層定位成「只做結構檢查」,語意判斷交給人 + `spectra analyze`/`drift` —— 這個 gate 設計上就是要當一個便宜的強制觸發點,不是要當正確性的仲裁者。遇到邊界情況,補一個 doc/spec stub 永遠是可用的逃生門(這是已知的權衡,不是設計缺陷)。
- [沒開 Redis service] → 若之後 Celery task 落地卻忘記把 service 加回來,相關測試會直接連線失敗(connection refused)大聲報錯,不會是靜默錯誤 —— 屬於可接受的快速失敗,不是隱藏風險。

## Migration Plan

只新增一個檔案(`.github/workflows/ci.yml`);要退回的話刪掉檔案即可,沒有其他要遷移或回滾的東西。這次程式碼變更本身不含 branch protection 設定 —— 要讓這個 gate 真正生效,還需要在 `main`/`develop` 開 branch protection 要求這些 check 通過,這件事標記成 tasks.md 裡的人工後續(GitHub repo 設定,不屬於這次程式碼 diff)。
