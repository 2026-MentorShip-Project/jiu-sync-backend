## Context

`config/exceptions.py` 是全站共用的錯誤格式基礎設施(`api-error-format`,已歸檔)。`custom_exception_handler` 目前用 `_flatten_message`/`_find_leaf` 把任何驗證錯誤壓成單一字串,只取第一個失敗欄位;`code` 除非 view 透過 `ApiError` 明確指定,否則一律 `None`。`apps.accounts` 已經有一組成熟的 `ApiError(message, code="XXX", status_code=NNN)` 業務 code 慣例(`INVALID_ID_TOKEN`、`EMAIL_ALREADY_IN_USE`、`INVALID_REFRESH_TOKEN`、`REFRESH_TOKEN_NOT_YOURS`);`apps.events` 目前完全沒有業務 code,全部驗證錯誤走 DRF 內建 `serializers.ValidationError`。詳細動機見 proposal.md - Why(前端與後端多輪討論收斂的最終方案,原本考慮過「每條驗證規則配專屬 code」,最後確認 400 只需要 `field`+`message`,不需要 code)。

## Goals / Non-Goals

**Goals:**
- 400 驗證錯誤能一次列出全部失敗欄位(`errors` 陣列),含巢狀欄位路徑,且每筆各自帶有語意化的 `code`
- 401/403/404/500 在沒有既有業務 code 時,有一個穩定、可預期的預設 code,不再一律 `null`
- `GET`/`PATCH /api/events/{id}` 補上活動專屬的業務 code(`EVENT_NOT_FOUND`/`LINK_EXPIRED`/`EVENT_NOT_ACTIVE`)

**Non-Goals:**
- 不改變 `apps.accounts` 既有的業務 code,不重新命名、不調整涵蓋範圍
- 不改變任何驗證規則的判斷邏輯本身(長度上限、必填與否、時間比較等)——這次改動只在「同一個驗證失敗要附帶什麼 code」,不改「什麼情況算失敗」
- `PATCH` 非擁有者的 403 維持通用 `FORBIDDEN`,不配專屬 code(跟已有明確業務語意的既有 code 如 `REFRESH_TOKEN_NOT_YOURS` 不同,這是新情境,刻意維持粗粒度)

> **修訂記錄(2026-09-20)**:本節最初版本寫的是「不新增任何欄位驗證規則專屬的 code」「不改變 410 既有行為」,前端進一步確認需求後推翻了這兩點,現在的 Goals/Non-Goals 已經是修訂後的版本。詳細理由見 proposal.md 的修訂記錄段落。

## Decisions

### D1. `errors` 陣列取代「只抓第一個欄位」的 `_flatten_message`
`custom_exception_handler` 目前只在 `response.data` 是 dict 且沒有 `"detail"` 鍵時,取第一個 key 當作唯一錯誤。改成:對這種情況,遍歷 `response.data` 的**全部** key,每個 key 對應一或多個錯誤訊息(取第一則),組成 `{"field": key, "code": ..., "message": 第一則訊息}`,全部收進 `errors` 陣列。頂層 `message`/`code` 維持是**第一個**錯誤的訊息/code(不是全部串接)——保留給只讀 `message`/`code`、不讀 `errors` 的既有呼叫端一個合理的向後相容行為,不是這次要打掉重練的東西。

> **修訂記錄(2026-09-20)**:`errors` 每筆原本只有 `field`/`message`,前端進一步確認需求後追加 `code`,見 D7。頂層 `code` 原本固定 `null`,現在比照 `message` 改成「第一筆的值」。

### D2. 巢狀陣列欄位用 `<欄位>[<索引>].<子欄位>` 路徑命名
DRF 對巢狀 `many=True` serializer(例如 `slots`)驗證失敗時,`response.data["slots"]` 會是一個 list,只有失敗的索引位置是非空 dict(例如 `[{}, {"date": ["此為必需欄位。"]}]` 代表第 0 筆沒問題、第 1 筆的 `date` 有問題)。展開邏輯:偵測到值是 list 時,逐一走訪索引,對每個非空 dict 元素,再取它裡面每個 key,組成 `"slots[<index>].<key>"` 當作 `field`。只處理一層巢狀(`slots[].<field>`),不處理更深的巢狀——目前 `apps.events`/`apps.accounts` 沒有更深的巢狀結構,不需要提前設計。

### D3. 401/403/404/500 的預設 code 用「狀態碼 → 固定字串」對照表
在 `custom_exception_handler` 裡,只有當 `code` 還是 `None` 時,才依 `response.status_code` 查表補上:`{400: None, 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 500: "SERVER_ERROR"}`。

判斷「code 是不是 None」不能只看「這是不是 ApiError」——`ApiError(message, code=None, status_code=...)` 的 `code` 參數本身是選填的,呼叫端可以合法地只給 `status_code`、不給 `code`。所以就算 `isinstance(exc, ApiError)` 為真,還是要再檢查 `exc.api_code is not None` 才能判斷「這個例外有沒有明確指定業務 code」;`api_code` 是 `None` 時,一樣要落到 `STATUS_CODE_DEFAULT_CODES` 查表,不能因為「用了 ApiError」就整支繞過查表(這是 2026-09-20 code review 抓到的實作 bug,已修正)。`Gone`(410)不在查表範圍內,`.get()` 對查無的狀態碼自然回傳 `None`,不需要額外處理。

### D4. `handler404`/`handler500` 直接寫死對應 code
這兩支函式是 Django URL resolver 層級,不會經過 `custom_exception_handler`,不共用 D3 的查表邏輯(維護一份 2 筆對照的常數字典沒有意義,直接寫死字串更直接)。`handler404` 回 `"NOT_FOUND"`,`handler500` 回 `"SERVER_ERROR"`。

### D5. `apps.accounts`/`apps.events` 的驗證規則判斷邏輯不動,但 code 相關的呼叫點會動
`apps.accounts` 既有的 `ApiError` 呼叫點不用改(D3 的查表邏輯只在 code 是 `None` 時才介入,不影響已指定 code 的路徑)。`apps.events` 的驗證**規則本身**(長度上限、必填與否、時間比較等判斷邏輯)不變。

> **修訂記錄(2026-09-20)**:原本這條決策是「views/serializers 完全不動」,前端確認需要每條規則配專屬 code 後,不再成立——`apps.events` 手動 `raise ValidationError(...)` 的驗證規則(`hostNickname` 加權長度、`responseDeadline` 早於現在、`slots` 數量)補上明確 `code=`;`GET`/`PATCH /api/events/{id}` 改用 `ApiError`/`Gone` 明確指定業務 code,不再用 `get_object_or_404`/泛用 `ValidationError`(見 D8)。改的是「附帶什麼 code」,不是「判斷邏輯本身」,跟 Non-Goals 不衝突。

### D7. 欄位驗證的 code 兩種來源:手動指定 + 對照表 fallback
兩種欄位驗證各有不同的 code 取得方式:
- **手動 `raise ValidationError(..., code=...)` 的業務規則**(例如 `_validate_host_nickname_weighted_length`、`_validate_response_deadline_in_future`、`validate_slots`):拋出當下直接給正確的語意化 code,不需要額外對照。
- **純宣告式驗證**(`max_length=`、`required=`、`EmailField` 格式驗證等,沒有自訂 `validate_<field>` 方法):DRF 自動產生的 `ErrorDetail` 已經帶有 `.code`(例如 `"required"`、`"max_length"`、`"invalid"`),但是 DRF 自己的通用字串,不是我們要的語意化 code。新增 `FIELD_CODE_OVERRIDES`(`(欄位名, DRF 原始 code) → 語意化 code`)在 `_build_errors` 裡查表轉換;查無對應則直接沿用 DRF 原始 code 當 fallback,不會讓 `code` 消失。巢狀 `slots[].<subfield>` 欄位另外用 `NESTED_SUBFIELD_CODE_OVERRIDES`(只用子欄位名比對,不分 DRF 原始 code 是 required 還是 invalid),因為前端這個層級只要求一個 code,不需要更細的區分。

技術細節:要拿到 `.code`,`_find_leaf`(原本遞迴找到 leaf 就立刻轉成字串)改寫成 `_find_leaf_detail`(遞迴找到 leaf 但保留原始 `ErrorDetail` 物件,呼叫端自己決定何時 `str()`、何時讀 `.code`)。

替代方案考慮過:讓每個欄位都手動宣告 `validate_<field>` 方法、自己 `raise` 帶 code——工作量大很多(連 `title`/`description`/`location` 這種純長度限制的欄位都要各自寫一個 validate 方法),而且 DRF 原生已經有 `.code` 可用,查表轉換比重寫驗證邏輯便宜很多。

### D8. `GET`/`PATCH /api/events/{id}` 的業務 code 直接用既有的 `ApiError`/`Gone` 機制
不需要新機制——`apps.accounts` 已經證明 `ApiError(message, code="XXX", status_code=NNN)`/`Gone(message, code="XXX")` 是好用的既有慣例。`GET`:查無資料時 `ApiError(..., code="EVENT_NOT_FOUND", status_code=404)`(取代 `get_object_or_404`,那樣拿到的是泛用 `NotFound`);算出 `displayStatus == "link_expired"` 時 `Gone(..., code="LINK_EXPIRED")`,直接複用既有的 `lifecycle.compute_display_status` 純函式,不新增判斷邏輯。`PATCH`:非 active 狀態改用 `ApiError(..., code="EVENT_NOT_ACTIVE", status_code=409)`(取代原本 400 的 `serializers.ValidationError`);非擁有者的 403 維持原本的 `PermissionDenied`(不動,自然吃到 D3 的通用 `FORBIDDEN` 預設值)。`GET`/`PATCH` 共用的活動查找邏輯抽成模組層級的 `_get_event_or_404` 函式,避免兩個 method 各寫一次。

### D6. `STATUS_CODE_DEFAULT_MESSAGES`:「detail」形狀分支的 message 也換成固定中文
D3 的狀態碼查表只補了 `code`,「detail」形狀分支(DRF/simplejwt 內建、無 `ApiError` 包裝的例外,例如 `NotAuthenticated`、`PermissionDenied`、`NotFound`、simplejwt 的 `AuthenticationFailed`)的 `message` 這次之前還是直接沿用原始 `.detail`,而這些例外的預設文字都是英文(例如 `"Given token not valid for any token type"`)。前端擔心會直接拿 `message` 渲染給使用者看,英文字串穿透出去體驗不一致。

新增一份平行的對照表 `STATUS_CODE_DEFAULT_MESSAGES`(`{401: "驗證失敗，請重新登入", 403: "沒有權限執行此操作", 404: "找不到這筆資料", 500: "伺服器發生未預期的錯誤"}`),只套用在「detail」形狀分支(`custom_exception_handler` 裡 `isinstance(data, dict) and "detail" not in data` 為否的那一支)。400 刻意不在表裡——這個分支理論上可能出現的 400(例如非 dict 形狀的 `ValidationError`)極罕見,沿用既有 `_flatten_message` 行為即可。ValidationError 的欄位訊息(`errors` 陣列、頂層 `message`)完全不受影響——那些本來就是我們自己 serializer 寫的中文文案,不會被這份表覆蓋(這份表只在 else 分支查,ValidationError 走的是另一支 `if` 分支)。

## Risks / Trade-offs

- **[風險] `errors` 陣列只取每個欄位的「第一則」訊息,DRF 有時對同一欄位會有多條錯誤訊息(例如同時觸發 `required` 跟自訂 validator)** → 緩解:這是既有 `_find_leaf` 就有的行為(只取第一個),這次沒有改變這個既有慣例,不是新引入的限制。
- **[風險] 巢狀路徑展開邏輯只處理一層(`slots[].field`),未來如果出現更深的巢狀結構會不夠用** → 緩解:目前專案沒有這種需求,提前設計反而是過度工程;真的出現時再擴充展開邏輯,屆時的資料結構會更清楚,現在猜測沒有意義。
- **[風險] 401/403/404/500 原本讀到 `code: null` 的既有呼叫端(如果有的話),現在會讀到固定字串,行為改變** → 緩解:目前專案前端還在對接階段,沒有已上線依賴舊行為的呼叫端;`specs/api-error-format/spec.md` 的 REMOVED/ADDED 已經明確記錄這個行為變更與遷移說明。
- **[風險] `FIELD_CODE_OVERRIDES`/`NESTED_SUBFIELD_CODE_OVERRIDES` 是全域、扁平的對照表,耦合了 `exceptions.py`(共用基礎設施)跟各 app 的欄位語意** → 緩解:目前專案規模小,欄位名跨 serializer 沒有衝突風險;若未來規模變大、欄位名開始碰撞,再考慮把對照表拆到各 app 自己維護、`custom_exception_handler` 改成可註冊擴充點,現在做這個抽象是過度工程。
- **[風險] `GET /api/events/{id}` 的 410/`LINK_EXPIRED` 分支目前系統沒有 finalize/cancel 端點,無法透過任何真實 API 流程觸發** → 緩解:直接建立測試資料(`Event.objects.create(status=..., cancelled_at=...)`)驗證,不依賴真的走過 finalize/cancel 流程產生資料——這是刻意的決定(見 proposal.md 修訂記錄),換取這個分支提前就位,之後 finalize/cancel 端點做出來時不用回頭補。
- **[已修正的風險] `_find_leaf_detail` 原本假設 dict/list 一定有內容,遇到空容器(`{"field": []}`/`{"field": {}}`)會直接拋 `IndexError`/`StopIteration`,讓一個原本該回 400 的驗證錯誤,因為錯誤格式化程式自己出包變成 500** → 2026-09-20 code review 抓到並修正:`_find_leaf_detail` 對空容器回傳 `None`,`_build_errors` 遇到 `None` 直接略過該欄位,不硬湊假訊息,也不影響其他正常欄位。DRF 自己的驗證邏輯不會產生這種形狀,但 `custom_exception_handler` 是全站共用基礎設施,不能假設所有呼叫端(未來的自訂 validator、第三方套件)都只會傳入「合法」輸入。

## Migration Plan

純邏輯改動,不涉及資料庫、不需要 migration。部署後任何後續請求立即套用新格式,無需資料回填或平行運行期。若需回滾,`config/exceptions.py` 改回舊版 `_flatten_message`/`_find_leaf`、拿掉狀態碼查表即可,不影響任何已儲存資料。
