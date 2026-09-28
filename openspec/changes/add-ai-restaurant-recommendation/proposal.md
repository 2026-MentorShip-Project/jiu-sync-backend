## Why

PRD(前端 repo `docs/auth-&-AI-feature-2026-0901/`)把產品收斂成「成團後由主揪發起的 AI 選餐廳」,前端 `AIRecommendFlow` 的偏好表單與結果畫面已經做好,但目前全是 mock 資料,後端沒有任何推薦 API(`apps.recommendations` 只有空殼)。Perplexity 呼叫有實際費用,因此同時需要每人每月的使用次數上限,且上限必須在併發下仍然成立。

## What Changes

- 新增 `POST /api/events/{id}/restaurant-recommendations/`:活動擁有者在活動已定案且聚會日期未過時,送出偏好條件(全部選填),後端補上活動既有資訊(地點、出席人數、定案時段)後呼叫 Perplexity Agent API,回傳 1–5 間結構化推薦餐廳、實際採用的條件(重述需求)與剩餘次數
- 新增 `GET /api/me/ai-recommendation-quota/`:回傳當月額度(上限、已用、剩餘、是否可用、重置時間)與服務是否可用
- 使用次數上限:每人每月(台灣時間自然月)20 次,上限可由設定調整;只有成功產生推薦才計次,失敗/逾時/驗證錯誤不計次;併發請求不可超用;同一活動已有進行中的請求時拒絕新請求
- 推薦相關事件輸出結構化 JSON log(固定 `event` 名稱與欄位),供 Loki 以 LogQL 計算次數、失敗率、延遲、費用;僅套用於推薦模組,不改變其他模組的 log 格式
- 新增推薦紀錄資料表:每次呼叫一筆,保存輸入條件、結果、狀態、錯誤原因、token 用量、耗時,作為計次來源與稽核紀錄
- 新錯誤代碼:`LOCATION_REQUIRED`(400)、`AI_RECOMMENDATION_QUOTA_EXCEEDED`(403)、`EVENT_ALREADY_PAST`(409)、`AI_RECOMMENDATION_IN_PROGRESS`(409)、`AI_RECOMMENDATION_UPSTREAM_FAILED`(502)、`AI_RECOMMENDATION_UNAVAILABLE`(503)、`AI_RECOMMENDATION_UPSTREAM_TIMEOUT`(504);沿用既有 `EVENT_NOT_FOUND`、`EVENT_NOT_FINALIZED`、`LINK_EXPIRED`、`FORBIDDEN`
- `RECOMMENDATION_ENGINE` 預設值由 `google_places_gemini` 改為 `perplexity`;設定成未實作的引擎時啟動即報錯
- 正式環境 gunicorn 改為 gthread worker、timeout 由 30 秒調整為 60 秒,以容納同步的 AI 呼叫

未涵蓋(明確排除):推薦歷史紀錄查詢 API;主揪選定一間餐廳並寫回活動;Google Places + Gemini 引擎(PRD 方案 A);非同步(背景工作 + 輪詢)呼叫模式;管理員手動調整個別使用者額度的介面;全站 JSON log、Prometheus metrics 與 Grafana/Loki 收集端建置(另開觀測性 change)。以上之後要做皆為純新增,不需改動本次架構。

## Capabilities

### New Capabilities

- `restaurant-recommendations`:活動擁有者取得 AI 餐廳推薦、每月使用額度的計算與查詢

### Modified Capabilities

(無 — 不改變既有活動、登入、錯誤格式的行為;新錯誤代碼沿用既有 `api-error-format` 的統一形狀)

## Impact

- `apps/recommendations/`:新 model + migration、serializer、view、Perplexity 引擎模組、prompt 組裝、結果驗證
- `config/urls.py`:掛上 quota 查詢路由;`apps/events/urls.py` 或 recommendations 的 url 掛上活動底下的推薦路由
- `config/settings/base.py`:新增 `LOGGING`(只設定 `apps.recommendations` logger)、`RECOMMENDATION_ENGINE` 預設值與註解、`AI_RECOMMENDATION_QUOTA_PER_USER`、Perplexity 模型/逾時設定
- `Dockerfile`:gunicorn 參數
- `pyproject.toml`:HTTP client 依賴由 `google-auth[requests]` 的間接依賴改為明確宣告
- 外部依賴:Perplexity Agent API(`POST https://api.perplexity.ai/v1/agent`),需要 EC2 `.env` 設定 `PERPLEXITY_API_KEY`;EC2 host 上 nginx 的 `proxy_read_timeout` 需 ≥ 60 秒(跨 repo)
- 前端需配合:新增 `restrictions`(忌口/過敏)欄位、`cuisines` 可自訂、串接 quota API、請求進行中停用按鈕
