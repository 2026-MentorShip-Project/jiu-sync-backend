## ADDED Requirements

### Requirement: 401/403/5xx 錯誤回應 SHALL 記錄後端可查驗的 log
系統 SHALL 在回應狀態碼為 401、403 或 5xx 時,額外記錄一筆後端可查驗的 log(不是回給前端 body 的一部分),內容至少包含:請求方法與路徑、HTTP 狀態碼、`code`、`message`,以及請求已登入時的使用者 id。400、404、409、410 這類正常業務流程(欄位驗證失敗、資料找不到、狀態衝突、連結失效)SHALL 不記錄,避免雜訊。5xx 的 log SHALL 額外包含原始例外的 traceback。

#### Scenario: 401 記錄 WARNING 等級的 log
- **WHEN** 一個請求觸發 401(例如缺少或無效的身分憑證)
- **THEN** 後端記錄一筆 WARNING 等級的 log,內容含請求路徑、狀態碼 401、`code`,以及使用者已登入時的 id

#### Scenario: 403 記錄 WARNING 等級的 log
- **WHEN** 一個請求觸發 403(例如非活動擁有者嘗試執行擁有者專屬操作)
- **THEN** 後端記錄一筆 WARNING 等級的 log

#### Scenario: 業務程式碼明確拋出的 500 記錄 ERROR 等級的 log
- **WHEN** view 層透過 `ApiError` 明確指定 `status_code=500`
- **THEN** 後端記錄一筆 ERROR 等級的 log,不是 WARNING

#### Scenario: 正常業務流程的錯誤不記錄
- **WHEN** 一個請求觸發 400(欄位驗證失敗)、404(資料不存在)、409(狀態衝突)或 410(連結已失效)
- **THEN** 後端不記錄任何 log

#### Scenario: 走不到 custom_exception_handler 的真正未預期例外記錄完整 traceback
- **WHEN** 發生一個連 DRF 例外處理都攔不到的伺服器錯誤(程式本身的 bug,走 Django 層級的 500 處理)
- **THEN** 後端記錄一筆 ERROR 等級的 log,且包含原始例外的完整 traceback,方便排查根因
