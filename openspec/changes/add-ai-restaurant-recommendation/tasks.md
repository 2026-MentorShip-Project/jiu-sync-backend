> TDD 排法:RED(先寫會失敗的測試)→ GREEN(只寫剛好讓它通過的最小實作)→ 視情況 REFACTOR。Seam 走 HTTP 層(DRF `APIClient`);引擎在 view 測試中以 fixture 替換 `get_engine()`,Perplexity 引擎本身以 `monkeypatch` 替換 `requests.post` 做單元測試。CI 永遠不打真的 Perplexity。
>
> 每個 task 用全新 context 執行,呼叫 `/mattpocock-skills:implement` 時指定本檔對應段落;執行前先讀 `docs/agents/incident-log/` 最近幾天的紀錄。

## 0. 環境健檢與 Perplexity 實測(新外部服務 + 依賴明確化)

- [x] 0.1 環境健檢:`git status`/`git log -1` 正常且位於 `feature/ai-pick`;`uv run python -c "import requests; print(requests.__version__)"` 成功;在 `pyproject.toml` 明確宣告 `requests` 並 `uv lock` 後 `uv sync --locked` 成功;確認本機 `.env` 有 `PERPLEXITY_API_KEY`(只檢查非空,不印出值)— (auto)
- [x] 0.2 Perplexity 實測:在 scratchpad(不進 repo)寫一次性腳本,依 design.md D8 的 request 形狀(instructions + input + `web_search` + json_schema)打 `POST https://api.perplexity.ai/v1/agent`,至少 5 組條件(不同地點、人數、有無忌口、條件嚴格到可能不足 3 間),每組記錄:耗時秒數、`status`、是否通過 schema、餐廳數、`sourceUrl` 名稱比對命中率、`usage.cost.total_cost`。比較至少 2 種 `model`/`preset`,選定 `PERPLEXITY_MODEL` 預設值。結果摘要寫進本 change 目錄 `notes/perplexity-benchmark.md`(不含 API key)— (manual)需要真實 key 與費用,完成後停下讓我確認選定的模型
- [x] 0.2b 補充重測(A 方案):依 design.md D8 放寬後的 System Prompt 規則 1,以 `preset:low` 重跑 C1–C5(最多 6 次呼叫),記錄同 0.2 的欄位,並比對放寬前後的餐廳數、`notes` 內容與是否出現疑似編造(店名不在任何 `search_results` title 中的比例)。結果追加到 `notes/perplexity-benchmark.md` — (manual)需要真實 key 與費用,完成後停下讓我確認
- [x] 0.3 關卡:若 0.2/0.2b 的常態耗時 > 45 秒、schema 通過率明顯不穩,或放寬後一般條件(C1–C4)仍常態少於 3 間,**停止 implement**,回頭走 `opsx:update` 重新 grill design.md D10(同步 vs 非同步)/ D8 — (manual)

## 1. 額度查詢 API(資料表 + 計次規則 + `GET /api/me/ai-recommendation-quota/`)

- [x] 1.1 [RED] 在 `apps/recommendations/tests/test_quota.py` 寫測試(直接建 `RestaurantRecommendationRequest` 紀錄當前置資料),涵蓋:① 無紀錄 → `limit 20, used 0, remaining 20, available true`、`period` 為台灣時間當月、`resetsAt` 為下月 1 日 00:00+08:00;② 3 筆當月 `succeeded` → `used 3`;③ `failed` 不計;④ 建立 < 5 分鐘的 `pending` 計入,> 5 分鐘不計;⑤ 上月的 `succeeded` 不計(含台灣時間 9/30 23:59 vs 10/1 00:00 邊界,用固定時間);⑥ 20 筆 → `remaining 0, available false`;⑦ 別的使用者的紀錄不計;⑧ `override_settings(AI_RECOMMENDATION_QUOTA_PER_USER=5)` 生效;⑨ `PERPLEXITY_API_KEY=""` → `serviceAvailable false` 且仍 200;⑩ 未登入 → 401。確認 FAIL — (auto) `pytest` 顯示 FAIL
- [x] 1.2 [GREEN] 實作:`RestaurantRecommendationRequest` model + migration(design.md D3 欄位與索引)、額度計算函式(D3/D5/D6)、quota view 與 `config/urls.py` 路由、`AI_RECOMMENDATION_QUOTA_PER_USER` 設定、引擎 `is_available()` 的最小版本(只判斷 key 是否為空)。1.1 轉綠 — (auto) `pytest apps/recommendations` 全綠、`makemigrations --check` 無遺漏

## 1B. 結構化 JSON log 輸出(formatter + LOGGING 設定)

- [ ] 1B.1 [RED] 在 `apps/recommendations/tests/test_logging.py` 寫測試,涵蓋:① `JsonFormatter.format()` 對帶 `extra` 的 record 輸出單行、可 `json.loads` 的字串,含 `timestamp`(ISO 8601 含時區)、`level`、`logger`、`event`;② 數值欄位維持 JSON 數字(`latency_ms: 1234` 不是 `"1234"`),`None` 輸出為 `null`;③ 非白名單的 `extra` 欄位不會被輸出;④ 帶 `exc_info` 時輸出 `exc_type`/`exc_message`/`traceback`;⑤ 中文欄位值不被跳脫成 `\uXXXX`(`ensure_ascii=False`);⑥ 經由 Django 設定的 `apps.recommendations` logger 實際輸出到 stdout 的是 JSON(`capsys`),而其他 logger(例如 `apps.events`)的輸出格式不變。確認 FAIL — (auto) `pytest` 顯示 FAIL
- [ ] 1B.2 [GREEN] 實作:`apps/recommendations/logging.py` 的 `JsonFormatter`(design.md D11)、`config/settings/base.py` 的 `LOGGING`(只設定 `apps.recommendations`,`propagate: False`)。1B.1 轉綠,全套 `pytest -q` 仍全綠 — (auto)

## 2. 推薦 API 主流程(假引擎;前置檢查 + 條件解析 + 預留/確認/釋放)

- [ ] 2.1 [RED] 在 `apps/recommendations/tests/test_views.py` 寫測試(fixture 替換 `get_engine()` 為可控制回傳/丟例外的假引擎),涵蓋:① 成功 → 201,回應含 `id`、`restaurants`(每間有 `id`)、`notes`、`resolvedPreferences`、`quota`(`used` 已 +1),DB 紀錄為 `succeeded` 且存 `preferences`/`result`/`usage`/`model`/`latency_ms`;② 未登入 401;③ 活動不存在 404 `EVENT_NOT_FOUND`;④ 非擁有者 403 且假引擎未被呼叫;⑤ 投票中/截止待定案/已取消 → 409 `EVENT_NOT_FINALIZED`;⑥ 定案時段日期早於今天 → 409 `EVENT_ALREADY_PAST`;⑦ 定案超過 7 天 → 410 `LINK_EXPIRED`;⑧ key 未設定 → 503 `AI_RECOMMENDATION_UNAVAILABLE` 且無任何紀錄;⑨ 欄位驗證:不合法 enum、`situational` 重複、`cuisines` 6 項、單項 21 字、`restrictions` 單項 31 字、`customPrompt` 201 字、全空白字串視同未填 → 400 且 `errors` 列出所有欄位、無紀錄;⑩ 空 body + 活動有地點 → `locationSource: "event"`;⑪ 請求地點優先 → `locationSource: "request"`;⑫ 兩邊都無地點 → 400 `LOCATION_REQUIRED`、無紀錄;⑬ 未填 `partySize` → 以定案時段 `available` 且未軟刪除的回覆數帶入;⑭ 假引擎丟 `UpstreamTimeout` → 504 `AI_RECOMMENDATION_UPSTREAM_TIMEOUT`,紀錄 `failed`、`error_code` 正確、額度未增加;⑮ `UpstreamHTTPError`/`UpstreamInvalidResponse`/`NoUsableResults` → 502 `AI_RECOMMENDATION_UPSTREAM_FAILED`,同上不計次;`NoUsableResults` 的 502 body 含 `notes`(有值與 null 各一例),其他兩種不含;⑯ 假引擎丟非預期 `RuntimeError` → 500,紀錄 `failed`/`UNEXPECTED_ERROR`(不殘留 `pending`);⑰ 失敗後立即重試可成功;⑱ 重新推薦:同活動第一次成功後再送一次 → 201、`used` 再 +1、DB 有兩筆 `succeeded` 且第一筆 `result` 未被改動;⑲ log(`caplog`):成功時有一筆 `event=ai_rec.succeeded` 且帶 spec 列出的欄位、`latency_ms`/`cost_usd` 為數字;逾時時有 `ai_rec.failed` 帶 `error_code` 與例外資訊;key 未設定時有 `ai_rec.unavailable`;帶 `customPrompt` 的請求所有 log 訊息與欄位都不含該文字。確認 FAIL — (auto) `pytest` 顯示 FAIL
- [ ] 2.2 [GREEN] 實作:請求 serializer(spec 欄位與限制)、條件解析(D9 活動資訊帶入)、引擎介面與例外階層(D7,此時只有假引擎在測試中使用)、view 依 D2 檢查順序與 D4 流程、錯誤碼對應、D11 的 log 呼叫、`apps/events/urls.py` 路由。2.1 轉綠 — (auto) `pytest apps/recommendations` 全綠

## 3. 額度併發安全與連點防護

- [ ] 3.1 [RED] 在 `apps/recommendations/tests/test_concurrency.py` 寫測試(`@pytest.mark.django_db(transaction=True)` + `threading.Barrier`,寫法比照既有 `test_comment_rate_limit_concurrent_legit_requests_only_one_succeeds`),涵蓋:① 已用 20 次 → 403 `AI_RECOMMENDATION_QUOTA_EXCEEDED`,假引擎未被呼叫;② 已用 19 次,對 3 個不同活動同時送出 → 恰好 1 個 201、其餘 403,當月 `succeeded` = 20;③ 同一活動同時送出 2 個 → 1 個 201、1 個 409 `AI_RECOMMENDATION_IN_PROGRESS`,假引擎只被呼叫 1 次(假引擎內用 Event 等待另一請求抵達,確保真的重疊);④ 同一活動有 > 5 分鐘的殘留 `pending` → 新請求可正常進行;⑤ 不同使用者的請求互不阻擋;⑥ log:額度拒絕有 `ai_rec.quota_denied`(含 `used`/`limit`/`period`),進行中拒絕有 `ai_rec.in_progress_denied`。確認 FAIL(或在 2.2 已部分通過的項目,說明原因)— (auto) `pytest` 顯示 FAIL
- [ ] 3.2 [GREEN] 實作/修正:D4 的 `select_for_update` 鎖 `User` 列 + 鎖內計數與建立 `pending` + 同活動進行中檢查;狀態轉換用條件式 `update` 保證只轉換一次。3.1 轉綠,且 1.x/2.x 測試仍全綠 — (auto) `pytest apps/recommendations` 全綠

## 4. Perplexity 引擎實作(prompt、schema、解析、來源比對)

- [ ] 4.1 [RED] 在 `apps/recommendations/tests/test_perplexity_engine.py` 寫單元測試(`monkeypatch` 替換 `requests.post`,回應樣本以 0.2 實測記錄的真實結構為準),涵蓋:① 送出的 request:URL、`Authorization` header、`PERPLEXITY_MODEL=preset:low` 時送 `preset: "low"` 且不送 `model`、設為 `openai/xxx` 時送 `model` 且不送 `preset`、格式不合法時 `ImproperlyConfigured`、`tools` 含 `web_search`、`response_format` 為 json_schema 且 schema **不含任何 url 欄位**、`timeout=(5, 45)`;② User Prompt 含地點/人數/用餐時間/enum 對應描述,自由文字只出現在 `【使用者補充】…【補充結束】` 區塊內;未填欄位不出現;③ 正常回應 → 解析出餐廳、camelCase、依序 `r1`…;④ 7 間 → 取前 5;⑤ 缺 `name` 或 `address`(含全空白)的項目被丟掉;⑥ 全部被丟 → `NoUsableResults` 且攜帶模型的 `notes`;⑥b `output[]` 含 `fetch_url_results` 等未知 type 時忽略、多個 `search_results` 區塊都納入比對;⑥c DB/log 的 `model` 取自回應而非設定;⑥d System Prompt 含放寬後的規則 1 文字;⑦ `requests.Timeout` → `UpstreamTimeout`;⑧ HTTP 5xx/4xx(含 429 `{"error":{...}}` body)→ `UpstreamHTTPError`(帶 status);⑨ `status: "failed"`/`"incomplete"`、非 JSON 文字、結構不符 → `UpstreamInvalidResponse`,且 error detail 截斷在 2,000 字;⑩ `sourceUrl`:名稱出現在 `search_results` 的 title → 取該 url;title 都沒有但出現在某筆 `snippet` → 取該筆 url;title 命中優先於 snippet 命中;全形/半形、大小寫差異仍命中;找不到 → `null`;不會回傳任何不在上游來源中的 url;⑪ `usage` 原樣回傳;⑫ `RECOMMENDATION_ENGINE="google_places_gemini"` 或未知值 → `ImproperlyConfigured`。確認 FAIL — (auto) `pytest` 顯示 FAIL
- [ ] 4.2 [GREEN] 實作:`apps/recommendations/engines/perplexity.py`(D8/D9)、`get_engine()` 選擇與 `AppConfig.ready()` 檢查(D7)、`PERPLEXITY_MODEL`(預設 `preset:low`)/`PERPLEXITY_TIMEOUT_SECONDS` 設定、`RECOMMENDATION_ENGINE` 預設改 `perplexity` 並更新 `config/settings/base.py` 註解。4.1 轉綠,全套 `pytest -q` 全綠 — (auto)
- [ ] 4.3 本機端到端:本機 `runserver` + 真實 key,用一個已定案、日期未過的活動打 `POST /api/events/{id}/restaurant-recommendations/`(空 body 與帶條件各一次),確認 201、`sourceUrl` 有值或為 null、`GET` quota 的 `used` 增加;再把 key 改錯確認回 502、`used` 不變 — (manual)需要真實 key

## 5. 正式環境設定

- [ ] 5.1 `Dockerfile` gunicorn 改為 `--worker-class gthread --workers 3 --threads 4 --timeout 60`(D10);`docker build` 成功且容器內 `gunicorn --check-config` 通過 — (auto)
- [ ] 5.2 EC2 部署前確認:host nginx `proxy_read_timeout` ≥ 60 秒;EC2 `.env` 加上 `PERPLEXITY_API_KEY`(以及 0.2 選定且與預設不同時的 `PERPLEXITY_MODEL`)— (manual,跨 repo / 正式環境)

## 6. 收尾

- [ ] 6.1 全套驗證:`pytest -q`、`ruff check .`、`manage.py check`、`makemigrations --check --dry-run` 皆乾淨;`openspec validate add-ai-restaurant-recommendation --strict` 通過 — (auto)
- [ ] 6.2 `/code-review` 自審,處理發現的真實問題 — (auto)
- [ ] 6.3 `spectra analyze` + `spectra drift` 確認實作與 design.md 一致、無落差;有落差先修正或 `ingest` — (auto)
- [ ] 6.4 整理給前端的串接說明(request/response 範例、錯誤碼表、`restrictions` 新欄位、quota API、進行中停用按鈕、「重新推薦也計次」),以及 design.md D11 的 LogQL/SQL 查詢,放在 PR 描述 — (manual)我確認後再轉給前端
