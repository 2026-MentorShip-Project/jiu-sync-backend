## Why

使用者發現全站除了回給前端的 `{message, code}` 之外,後端幾乎沒有留任何 log——出問題時後端自己查不到脈絡(誰打的、打哪支、什麼時候)。`api-error-format` capability 的既有規格其實已經隱含這件事(「真正未被攔截的伺服器錯誤…伺服器端的錯誤紀錄／日誌不受影響、照常完整記錄」),但實際程式碼從來沒有真的寫過對應的 log 呼叫,這次補上。

## What Changes

- `config/exceptions.py::custom_exception_handler`(全站 `ApiError`/`ValidationError` 等業務例外唯一的出口,所有 view 的 `raise` 最終都會流經這裡)補上集中式 log:401/403(可能是憑證被冒用/竄改)與 5xx 記一筆,內容含請求 method/路徑、使用者 id(已登入時)、HTTP 狀態碼、`code`、`message`。400/404/409/410 這類正常業務流程(使用者打錯、資料本來就找不到)不記,避免雜訊
- `handler500`(Django 層級、連 DRF 例外處理都攔不到的真正未預期例外/程式 bug)額外記 ERROR 並帶完整 `exc_info`(traceback)——這是最該留存記錄的一種
- 選擇在 `custom_exception_handler`/`handler500` 這兩個既有的共用出口集中加 log,不是逐一去改全站 32 處 `raise` 呼叫點:涵蓋現有與未來新增的 raise,不會漏,也不用在每個 view 重複寫

未涵蓋(明確排除):不改變任何回應給前端的 `{message, code}` 內容或狀態碼;不新增 `LOGGING` settings(Python logging 沒有設定時,`lastResort` handler 仍會把 WARNING/ERROR 輸出到 stderr,先確保 log 本身正確產生,格式化/集中收集是後續 infra 議題,不在本次 scope)。

## Capabilities

### New Capabilities

(無)

### Modified Capabilities

- `api-error-format`:補上「401/403/5xx 錯誤回應 SHALL 記錄後端可查驗的 log」需求,把既有規格裡「伺服器端的錯誤紀錄／日誌不受影響、照常完整記錄」這句話落實成真正的實作

## Impact

- 修改 `config/exceptions.py`:新增 `_log_error_response` helper、`_LOGGED_STATUS_CODES` 常數,`custom_exception_handler`/`handler500` 各自呼叫
- 新增測試於既有的 `config/tests/test_exceptions.py`(不需要新建檔案)
