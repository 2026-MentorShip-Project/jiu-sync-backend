## 1. 本地乾跑 CI 會跑的指令

- [x] 1.1 在目前 `HEAD` 下跑 `uv run ruff check .`、`uv run python manage.py makemigrations --check --dry-run`、`uv run python manage.py migrate`(對全新、空的 Postgres DB —— 本機既有的 `jiu_sync` dev DB 本身 migration 記錄跟實際 schema 有漂移,屬於既有環境問題、不在這次 scope,驗證時另開一個乾淨 throwaway DB 跑)、`uv run pytest --cov --cov-report=term-missing`,確認四條指令全部乾淨通過。(auto —— 看指令 exit code)
  - 發現 1:`pytest-cov` 沒裝(`--cov` 直接報 unrecognized arguments)。已 `uv add --dev pytest-cov` 補上。
  - 發現 2:本專案目前 `apps/**` 下的 `tests.py` 都只是 Django 預設的 2 行空殼、沒有任何真測試,`pytest` 在 0 個測試被收集時會回傳 exit code 5(視為失敗)。這代表現在直接把 `pytest ...` 接到 CI,連這個「純加 CI」的 PR 自己都會被自己的 `test` job 擋下。Task 2.1 的 `test` job 要包一層「exit code 5(no tests collected)當作通過,其他非 0 才算失敗」的判斷,等專案開始有真測試後這層判斷自然就不會再被觸發,不用之後拿掉。

## 2. `lint` + `test` job

- [x] 2.1 建立 `.github/workflows/ci.yml`,包含 `lint` job(`ruff check .`)與 `test` job(Postgres 16 service、`uv sync --locked`、migration 檢查、migrate、pytest+cov,pytest 那步要把 exit code 5「no tests collected」視為通過、其他非 0 才算失敗,見 task 1.1 發現2),外加 top-level 的 `concurrency`/`cancel-in-progress`、`permissions: contents: read`、`pull_request` 觸發條件(對 `main`+`develop`),對應 design.md 決策 1、2、6、7。驗證方式:push 這個分支,確認 GitHub Actions 上兩個 job 都出現且通過(或用 `workflow_dispatch` 手動觸發)。(manual —— 需要真的看一次 Actions run)
- [x] 2.2 驗證 `test` job 真的會在壞輸入上失敗:本機模擬「改一個 model 欄位但不跑 makemigrations」,確認 `makemigrations --check --dry-run` 回傳非 0,驗證完再還原。(auto —— 看指令 exit code)

## 3. `spec-sync` 結構檢查

- [x] 3.1 新增 `spec-sync` job:比對 PR base 跟 `HEAD` 的 diff,若命中 `apps/**`/`config/**` 路徑,要求同一份 diff 裡至少也命中一個 `openspec/specs/**` 或 `openspec/changes/**` 路徑,否則失敗。本機先用 `git diff --name-only origin/main...HEAD` 驗證兩種情境:這個分支本身(只動到 `openspec/changes/add-ci-pipeline/**`,沒動 `apps/**`,因為沒有 `apps/**` diff 需要被擋,job 應該過)、以及一個只改 `apps/**` 檔案、不動 spec/change 的 scratch commit(job 邏輯應該擋下它)。(auto —— script exit code,先在本機驗證過邏輯,再信任 Actions run)

## 4. `/healthz` endpoint

- [x] 4.1 新增 `config/health.py`(plain 200、不碰 DB,見 design.md Decision 10、11),在 `config/urls.py` 掛上 `path('healthz/', ...)`,並補一個對應的 unit test(用 `APIClient` 打 `/healthz/` 斷言 200)。(auto —— `pytest apps config -k healthz` 或對應測試路徑通過)

## 5. `integration` job(server 起得來 + k6 smoke test)

- [x] 5.1 在 `ci.yml` 新增 `integration` job:沿用 `test` job 的 Postgres service + `uv sync --locked` + `migrate`,接著背景執行 `uv run python manage.py runserver 0.0.0.0:8000`(對應 design.md Decision 8),把 server log 導到檔案。(auto —— job yaml 語法正確、本機可用同組指令手動跑一次確認 server 能背景啟動)
  - 本機驗證:用 docker-compose 的 Postgres(host port 5455)開一個 throwaway DB `jiu_sync_ci_verify`,`migrate` 乾淨通過;背景啟動 `runserver`(本機用 8001 避開一個已存在、與本次改動無關的 8000 背景 process)、PID 存檔、`ps -p` 確認存活。
- [x] 5.2 加上輪詢 `/healthz` 的迴圈(仿 Go 範例:重試數十次、每次間隔數秒,逾時失敗),失敗時 `cat` server log 再 `exit 1`。(auto —— 本機故意讓 server 啟動失敗一次,確認迴圈會逾時且印出 log)
  - 本機驗證:正常情境下第 1 次嘗試即 200、exit 0。故意把 `DATABASE_URL` 指向不存在的 port(59999)模擬啟動失敗,`runserver` 的 autoreloader thread 因連不上 DB 丟例外,但外層 process 不會馬上死、`/healthz` 也一直連不上 —— 完整跑滿 30 次迴圈後印出含 traceback 的 server log、exit code 1,驗證通過後即刻清掉背景 process。
- [x] 5.3 用 `grafana/setup-k6-action` 裝 k6,寫一個最小 k6 腳本打 `/healthz` 斷言 200(對應 design.md Decision 9),當作 E2E smoke test 步驟。(manual —— 需要一次真實 Actions run 確認 k6 腳本語法與 action 版本正確)
  - 本機用 `brew install k6`(裝到 v2.2.0)針對本機起的 server 實跑 `tests/k6/healthz-smoke.js`,`check` 100% 通過。`grafana/setup-k6-action@v1` 這個 GitHub Action 本身、以及 pin 的 `k6-version: "1.6.1"` 是否真的能在 Actions runner 上正確安裝,仍待一次真實 Actions run 驗證,所以 manual 標記保留。
- [x] 5.4 加上 `Stop server`(`if: always()`,依 pid 關閉背景 process)與 `Upload test results`(`actions/upload-artifact@v4`,`retention-days: 7`,上傳 server/k6 log)兩個步驟,對應 design.md Decision 12。(auto —— 本機模擬跑完整個腳本流程,確認 pid 檔案存在、kill 成功不報錯)
  - 本機驗證:`kill "$(cat server.pid)"` 兩種情境(server 正常/被我故意弄壞)都能成功關閉,關閉後 `lsof -i :<port>` 確認 port 已釋放、`ps` 確認找不到殘留的 python/uv runserver process(僅剩一個與本次驗證無關、驗證前就已存在的 8000 背景 process)。
- [ ] 5.5 端到端驗證:push 分支或 `workflow_dispatch` 觸發,確認 `integration` job 綠燈,且失敗案例(故意讓 healthz 逾時)會在 job log 看到完整 server log、且 artifact 頁籤有上傳檔案。(manual —— 需要兩次真實 Actions run:一次正常過、一次故意失敗)

## 6. Rollout 備註(人工,不屬於這次程式碼範圍)

- [ ] 6.1 這次 PR merge 後,到 `main` 與 `develop` 開 branch protection,要求 `lint`、`test`、`spec-sync`、`integration` 這四個 check 通過,這個 gate 才算真正生效,而不只是參考用。(manual —— GitHub repo 設定,不在這次 diff 裡)
