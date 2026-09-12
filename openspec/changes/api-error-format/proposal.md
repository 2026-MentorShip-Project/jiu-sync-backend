## Why

前端需要能對非 2xx 回應做程式化分支（例如同樣是 400，`EVENT_CANCELLED`／`EVENT_FINALIZED`／`VOTING_CLOSED` 要顯示不同文案），而目前 DRF 各種例外（`NotAuthenticated`、`PermissionDenied`、`ValidationError`……）預設回傳的形狀互不一致（有的是 `{"detail": "..."}`，有的是巢狀欄位錯誤字典）。前端已經有一份 Swagger 文件把錯誤格式定成統一的 `{message, code}`，這個格式要在後端真的落地成一個所有 view 都會經過的共用機制，而不是每支 view 自己各寫一套。跟 `google-sso-login` change 分開處理，因為這是跨越整個 API 的基礎設施，不是 auth 專屬的東西。

## What Changes

- 新增一個共用 DRF `EXCEPTION_HANDLER`：任何 view 拋出的例外，最終回應 body 都統一長 `{"message": "...", "code": "..." | null}`
- 新增 `ApiError(APIException)`：view 程式碼要回傳帶特定 `code` 的錯誤時（例如 `LINK_EXPIRED`、`EVENT_CANCELLED`）用這個類別拋，而不是自己手刻 `Response(...)`
- 新增 `Gone` 例外（HTTP 410）：DRF 內建沒有這個狀態碼，但 Swagger 文件裡「連結已失效」明確要求跟 404 分開
- DRF 內建例外（`NotAuthenticated`、`PermissionDenied`、`NotFound`、`ValidationError` 等）沒有自帶 `code` 的情況下，統一走 `code: null`，`message` 用 DRF 預設的 `detail` 內容
- 不改任何現有 view 的業務邏輯——這個 change 純粹是格式轉換層，`google-sso-login` 之後的 view 實作（`GoogleLoginView` 的 401 等）可以直接依賴這裡定義好的 `ApiError`

## Capabilities

### New Capabilities
- `api-error-format`：全站一致的非 2xx 錯誤回應格式

### Modified Capabilities

（無——這是新的、獨立的 capability）

## Impact

- `config/settings/base.py`：`REST_FRAMEWORK["EXCEPTION_HANDLER"]` 指到新的共用函式
- 新檔 `config/exceptions.py`（或等效位置）：`custom_exception_handler`、`ApiError`、`Gone`
- 不影響任何現有 app 的 model/view（目前都還是空殼或只有 `google-sso-login` 進行中的 auth 端點）；`google-sso-login` 的 Task 3 之後可以改用這裡的 `ApiError`／`Gone`，但那是那個 change 自己的事，這裡不動它的檔案
