## Context

`config/exceptions.py` 是全站共用的錯誤格式基礎設施(`api-error-format`,已歸檔)。`custom_exception_handler` 目前用 `_flatten_message`/`_find_leaf` 把任何驗證錯誤壓成單一字串,只取第一個失敗欄位;`code` 除非 view 透過 `ApiError` 明確指定,否則一律 `None`。`apps.accounts` 已經有一組成熟的 `ApiError(message, code="XXX", status_code=NNN)` 業務 code 慣例(`INVALID_ID_TOKEN`、`EMAIL_ALREADY_IN_USE`、`INVALID_REFRESH_TOKEN`、`REFRESH_TOKEN_NOT_YOURS`);`apps.events` 目前完全沒有業務 code,全部驗證錯誤走 DRF 內建 `serializers.ValidationError`。詳細動機見 proposal.md - Why(前端與後端多輪討論收斂的最終方案,原本考慮過「每條驗證規則配專屬 code」,最後確認 400 只需要 `field`+`message`,不需要 code)。

## Goals / Non-Goals

**Goals:**
- 400 驗證錯誤能一次列出全部失敗欄位(`errors` 陣列),含巢狀欄位路徑
- 401/403/404/500 在沒有既有業務 code 時,有一個穩定、可預期的預設 code,不再一律 `null`

**Non-Goals:**
- 不改變 `apps.accounts` 既有的業務 code,不重新命名、不調整涵蓋範圍
- 不新增任何欄位驗證規則專屬的 code(例如 `TITLE_TOO_LONG`)——已確認 400 靠 `field`+`message` 足夠
- 不改變 410(連結失效)既有行為
- 不改變任何驗證規則本身(長度上限、必填與否等)——這次改動集中在錯誤格式化層

## Decisions

### D1. `errors` 陣列取代「只抓第一個欄位」的 `_flatten_message`
`custom_exception_handler` 目前只在 `response.data` 是 dict 且沒有 `"detail"` 鍵時,取第一個 key 當作唯一錯誤。改成:對這種情況,遍歷 `response.data` 的**全部** key,每個 key 對應一或多個錯誤訊息(取第一則),組成 `{"field": key, "message": 第一則訊息}`,全部收進 `errors` 陣列。頂層 `message` 維持是**第一個**錯誤的訊息(不是全部串接)——保留給只讀 `message`、不讀 `errors` 的既有呼叫端一個合理的向後相容行為,不是這次要打掉重練的東西。

### D2. 巢狀陣列欄位用 `<欄位>[<索引>].<子欄位>` 路徑命名
DRF 對巢狀 `many=True` serializer(例如 `slots`)驗證失敗時,`response.data["slots"]` 會是一個 list,只有失敗的索引位置是非空 dict(例如 `[{}, {"date": ["此為必需欄位。"]}]` 代表第 0 筆沒問題、第 1 筆的 `date` 有問題)。展開邏輯:偵測到值是 list 時,逐一走訪索引,對每個非空 dict 元素,再取它裡面每個 key,組成 `"slots[<index>].<key>"` 當作 `field`。只處理一層巢狀(`slots[].<field>`),不處理更深的巢狀——目前 `apps.events`/`apps.accounts` 沒有更深的巢狀結構,不需要提前設計。

### D3. 401/403/404/500 的預設 code 用「狀態碼 → 固定字串」對照表
在 `custom_exception_handler` 裡,只有當 `code` 還是 `None`(不是 `ApiError` 指定的)時,才依 `response.status_code` 查表補上:`{400: None, 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 500: "SERVER_ERROR"}`。已經透過 `ApiError` 指定 code 的既有業務錯誤完全不受這個查表影響(先判斷 `isinstance(exc, ApiError)` 的既有邏輯不變)。

### D4. `handler404`/`handler500` 直接寫死對應 code
這兩支函式是 Django URL resolver 層級,不會經過 `custom_exception_handler`,不共用 D3 的查表邏輯(維護一份 2 筆對照的常數字典沒有意義,直接寫死字串更直接)。`handler404` 回 `"NOT_FOUND"`,`handler500` 回 `"SERVER_ERROR"`。

### D5. `apps.accounts`/`apps.events` 的 view、serializer 完全不動
這次改動 100% 集中在 `config/exceptions.py`。`apps.accounts` 既有的 `ApiError` 呼叫點不用改(D3 的查表邏輯只在 code 是 `None` 時才介入,不影響已指定 code 的路徑)。`apps.events` 的驗證規則(長度、必填、時間比較等)也不用改——它們拋的還是原本的 `serializers.ValidationError`,只是 `custom_exception_handler` 這一層處理方式變了。

## Risks / Trade-offs

- **[風險] `errors` 陣列只取每個欄位的「第一則」訊息,DRF 有時對同一欄位會有多條錯誤訊息(例如同時觸發 `required` 跟自訂 validator)** → 緩解:這是既有 `_find_leaf` 就有的行為(只取第一個),這次沒有改變這個既有慣例,不是新引入的限制。
- **[風險] 巢狀路徑展開邏輯只處理一層(`slots[].field`),未來如果出現更深的巢狀結構會不夠用** → 緩解:目前專案沒有這種需求,提前設計反而是過度工程;真的出現時再擴充展開邏輯,屆時的資料結構會更清楚,現在猜測沒有意義。
- **[風險] 401/403/404/500 原本讀到 `code: null` 的既有呼叫端(如果有的話),現在會讀到固定字串,行為改變** → 緩解:目前專案前端還在對接階段,沒有已上線依賴舊行為的呼叫端;`specs/api-error-format/spec.md` 的 REMOVED/ADDED 已經明確記錄這個行為變更與遷移說明。

## Migration Plan

純邏輯改動,不涉及資料庫、不需要 migration。部署後任何後續請求立即套用新格式,無需資料回填或平行運行期。若需回滾,`config/exceptions.py` 改回舊版 `_flatten_message`/`_find_leaf`、拿掉狀態碼查表即可,不影響任何已儲存資料。
