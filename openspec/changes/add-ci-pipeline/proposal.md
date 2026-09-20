## Why

`jiu-sync-backend`目前完全沒有 CI —— `.github/workflows/` 是空的。每個 PR 現在都是無檢查直接可合併:沒有自動 lint、沒有 test、沒有 migration drift 檢查,也沒有本專案自訂的強制規則(CLAUDE.md 第95行:改了 `apps/**` 實作卻沒同步改 `openspec/specs/**` 或 change 目錄文件,CI 應擋下 PR)。Bug、漂移的 migration、沒記錄的 spec drift 目前都能無阻擋進到 `main`/`develop`。

## What Changes

- 新增 `.github/workflows/ci.yml`,觸發條件為 `pull_request` 到 `main` 與 `develop`(+ `workflow_dispatch`),加上 `concurrency`(`cancel-in-progress: true`)與 `permissions: contents: read`。
- 拆成兩個平行 job:
  - `lint`:`ruff check .`
  - `test`:Postgres 16 service container、`uv sync --locked`、`manage.py makemigrations --check --dry-run`(擋下「改了 model 卻沒產生對應 migration」)、`manage.py migrate`,再跑 `pytest --cov --cov-report=term-missing`。
- 新增 `spec-sync` job,實作 CLAUDE.md 第95行的結構檢查:若 PR diff 動到 `apps/**`(或 `config/**`)卻沒同時動到 `openspec/specs/**` 或 `openspec/changes/**`,job 失敗。純 docs/CI/config 的 PR 因為沒有 `apps/**` diff,自動豁免。
- 不接 `CODECOV_TOKEN` / 不上傳外部 coverage —— coverage 只印在 job log,目前沒有對應 secret。
- 不開 Redis service container —— 目前 `apps/**` 下沒有任何程式碼在 import 或測試時真的碰 Celery/Redis(`config/celery.py` 是還沒被用到的空殼);等第一個 `shared_task` 落地再補。
- 新增 `config/health.py`,掛一個 `GET /healthz`(純 200,不碰 DB)—— 純 ops 用途,不是產品能力,不需要對應 spec。
- 新增 `integration` job:`test` job 跑過的 Postgres service + migrate 完成後,用 `manage.py runserver` 在背景起 server、輪詢 `/healthz` 確認活著,再用 k6 打一次 `/healthz` 當 smoke test。失敗時 `cat` server log 幫助除錯;`if: always()` 確保不管成功失敗都關掉背景 server,並把 log 上傳成 artifact(retention 7 天)。



## Capabilities

### New Capabilities
(無 —— 這次純屬 CI/tooling,不涉及產品可觀察行為變化)

### Modified Capabilities
(無)

這次 change 設定 `skip_specs: true` —— 改的是「怎麼驗證程式碼」,不是「系統做什麼」。

## Impact

- 新增檔案:`.github/workflows/ci.yml`、`config/health.py`、k6 測試腳本(路徑見 design.md)。
- 修改:`config/urls.py` 掛上 `/healthz` 路由。
- 影響範圍:之後每個要合進 `main`/`develop` 的 PR,合併前都會先跑 lint + migration 檢查 + test + spec-sync 結構檢查 + integration(server 真的起得來)。
- 不影響既有業務邏輯或現有 API 行為,只新增一個純 ops 用途的 `/healthz` endpoint。
