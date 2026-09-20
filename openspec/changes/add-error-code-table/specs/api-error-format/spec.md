## MODIFIED Requirements

### Requirement: 統一錯誤回應形狀
系統 SHALL 讓所有非 2xx 回應的 body 都是 `{"message": string, "code": string|null}` 這個形狀,不因為錯誤來自哪一支 view、哪一種例外類型而有不同的欄位結構。當錯誤是欄位驗證失敗時,回應 SHALL 額外包含一個 `errors` 欄位:陣列,每個元素為 `{"field": string, "message": string}`,列出**全部**驗證失敗的欄位,不只第一個。巢狀欄位(例如候選時段陣列裡某一筆的某個欄位)SHALL 用 `<陣列欄位名>[<索引>].<子欄位名>` 的路徑格式命名(例如 `slots[0].date`)。

#### Scenario: 未預期的例外仍符合統一形狀
- **WHEN** 任何 view 拋出一個沒有被特別處理過的例外(例如 DRF 內建的 `NotAuthenticated`)
- **THEN** 回應 body 仍是 `{"message": ..., "code": ...}` 形狀,不是 DRF 預設的 `{"detail": ...}`

#### Scenario: 多個欄位同時驗證失敗時全部列出
- **WHEN** 一次請求裡有兩個以上的欄位同時驗證失敗(例如標題超過長度上限、同時投票截止時間早於現在)
- **THEN** 回應的 `errors` 陣列包含每一個失敗欄位各自的 `{field, message}`,不是只回第一個

#### Scenario: 巢狀陣列欄位的錯誤用索引路徑命名
- **WHEN** 候選時段陣列裡第一筆(索引 0)的 `date` 欄位驗證失敗
- **THEN** `errors` 陣列裡該筆錯誤的 `field` 值為 `"slots[0].date"`

### Requirement: 未匹配任何路由的請求也符合統一格式
系統 SHALL 讓「沒有任何 URL 路由匹配」的請求(走不到任何 view)跟「未預期的伺服器錯誤」(走不到任何 view 的例外處理)回應也符合 `{message, code}` 形狀,不是框架預設的 HTML 錯誤頁。這兩種情況的 `code` SHALL 分別依前一條需求的狀態碼預設值規則,設為 `"NOT_FOUND"`(404)與 `"SERVER_ERROR"`(500)。

#### Scenario: 不存在的路徑回傳一致格式
- **WHEN** 請求的路徑沒有匹配任何已註冊的 URL
- **THEN** 系統回傳 404,body 是 `{"message": ..., "code": "NOT_FOUND"}` 形狀,不是 HTML

#### Scenario: 真正未被攔截的伺服器錯誤仍回一致格式，但不影響伺服器端可觀測性
- **WHEN** 發生一個連 DRF 例外處理都沒攔到的伺服器錯誤
- **THEN** 回應給使用者的 body 仍是 `{"message": ..., "code": "SERVER_ERROR"}` 形狀(狀態碼 500),但伺服器端的錯誤紀錄／日誌不受影響、照常完整記錄,不因為統一了對外格式而讓問題在伺服器端變得不可見

## REMOVED Requirements

### Requirement: 沒有機器可讀代碼時 code 明確為 null
**Reason**:原規則「無法歸類代碼時一律 null」過於單一,實務上 401/403/404/500 這幾種狀態碼在沒有既有業務 `ApiError` 指定 code 時,前端仍需要一個穩定可判斷的值(用於分流處理,例如 401 導向登入頁),固定 null 對這幾種狀態碼沒有實質幫助。拆成新規則,依狀態碼決定是否該有預設值。
**Migration**:400 驗證錯誤的 `code` 行為不變,仍是 `null`。401/403/404/500 在沒有既有業務 code 時,原本讀到 `code: null` 的呼叫端,現在會讀到 `"UNAUTHORIZED"`/`"FORBIDDEN"`/`"NOT_FOUND"`/`"SERVER_ERROR"`——若呼叫端曾經明確判斷 `code === null` 來識別這些狀態碼,需要改成判斷新的固定字串或直接改用 HTTP 狀態碼判斷。

## ADDED Requirements

### Requirement: 沒有業務代碼時,code 依狀態碼決定預設值
系統 SHALL 在 view 沒有透過 `ApiError` 明確指定 `code` 時,依回應的 HTTP 狀態碼決定 `code` 的預設值:400(驗證錯誤)明確設為 `null`,401 為 `"UNAUTHORIZED"`,403 為 `"FORBIDDEN"`,404 為 `"NOT_FOUND"`,500 為 `"SERVER_ERROR"`。已透過 `ApiError` 指定 `code` 的既有業務錯誤(例如 `apps.accounts` 的 `INVALID_ID_TOKEN`、`INVALID_REFRESH_TOKEN`、`REFRESH_TOKEN_NOT_YOURS`)SHALL 不受影響,繼續使用該 `ApiError` 指定的值。

#### Scenario: 400 驗證錯誤的 code 明確為 null
- **WHEN** 發生一個欄位驗證失敗的 400 錯誤,且沒有透過 `ApiError` 指定 code
- **THEN** 回應的 `code` 欄位為 `null`,錯誤細節透過 `errors` 陣列表達

#### Scenario: 401 沒有既有業務 code 時使用預設值
- **WHEN** 發生一個 401 未授權錯誤,且沒有透過 `ApiError` 指定 code
- **THEN** 回應的 `code` 欄位為 `"UNAUTHORIZED"`

#### Scenario: 403 沒有既有業務 code 時使用預設值
- **WHEN** 發生一個 403 權限不足錯誤,且沒有透過 `ApiError` 指定 code
- **THEN** 回應的 `code` 欄位為 `"FORBIDDEN"`

#### Scenario: 既有業務 code 不受影響
- **WHEN** view 透過 `ApiError` 明確指定了 `code`(例如 `INVALID_REFRESH_TOKEN`)
- **THEN** 回應的 `code` 欄位維持該指定值,不被狀態碼預設值覆蓋

### Requirement: 框架內建例外的 message 不得夾雜非中文原文
系統 SHALL 讓沒有透過 `ApiError`/驗證錯誤機制自訂訊息的例外(例如 DRF 內建的 `NotAuthenticated`、`PermissionDenied`、`NotFound`,或 JWT 驗證函式庫拋出的例外)回應固定的繁體中文 `message`,不得讓函式庫預設的英文原文(例如 `"Given token not valid for any token type"`)穿透到回應內容。401/403/404/500 分別對應固定文案。驗證錯誤的 `errors` 陣列與其訊息內容不受此規則影響,維持既有(已是中文的)欄位文案。

#### Scenario: 過期或格式錯誤的存取權杖回傳中文訊息
- **WHEN** 請求帶著過期或格式不正確的存取權杖,觸發 JWT 驗證函式庫的例外,且沒有透過 `ApiError` 自訂訊息
- **THEN** 回應的 `message` 是固定的中文文案,不包含函式庫原始的英文錯誤內容

#### Scenario: 未帶身分憑證回傳中文訊息
- **WHEN** 請求完全沒有帶身分憑證,觸發 DRF 內建的 `NotAuthenticated`
- **THEN** 回應的 `message` 是固定的中文文案,不是 DRF 預設的英文字串
