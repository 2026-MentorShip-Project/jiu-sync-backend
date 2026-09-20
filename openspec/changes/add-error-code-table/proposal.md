## Why

前端在對接 `POST /api/events`（與其他端點）的錯誤回應時發現:`code` 目前一律是 `null`,而且 400 驗證錯誤只會回傳**第一個**失敗欄位,無法一次標紅多個錯誤欄位。前端原本提議每條驗證規則配一個獨立語意 code(如 `TITLE_TOO_LONG`),但雙方討論後確認:400 靠 `field`+`message` 就足夠標欄位、顯示文案,不需要维護一份龐大的 code 對照表;真正需要固定 code 的是 401/403/404/500 這種「同一狀態碼可能有多種原因、且沒有既有業務 code」的情況。

## What Changes

- `POST /api/events` 等端點的 400 驗證錯誤回應新增 `errors` 欄位:`[{field, message}, ...]`,列出**全部**失敗欄位(不只第一個),巢狀欄位(例如候選時段)用 `slots[0].date` 這種路徑格式命名
- 401/403/404/500 在沒有既有業務 `ApiError`(例如 `apps.accounts` 已有的 `INVALID_REFRESH_TOKEN`)指定 code 的情況下,自動依狀態碼補上固定 code:`UNAUTHORIZED`/`FORBIDDEN`/`NOT_FOUND`/`SERVER_ERROR`
- Django URL 層級的 404(`handler404`,走不到任何 view)與未攔截的 500(`handler500`)也補上 `NOT_FOUND`/`SERVER_ERROR`,不再固定回 `null`
- 400 驗證錯誤本身的 `code` 維持 `null`(不新增機制),不受影響

未涵蓋(明確排除):不改變 `apps.accounts` 現有的業務 code(`INVALID_ID_TOKEN`/`EMAIL_ALREADY_IN_USE`/`INVALID_REFRESH_TOKEN`/`REFRESH_TOKEN_NOT_YOURS`);不新增任何欄位驗證規則專屬的 code(如 `TITLE_TOO_LONG`);不改變 410(連結失效)既有行為。

## Capabilities

### New Capabilities

(無)

### Modified Capabilities

- `api-error-format`:統一錯誤回應形狀新增可選的 `errors` 陣列欄位;401/403/404/500 無業務 code 時的預設行為從「固定 null」改為「依狀態碼補上固定 code」

## Impact

- 修改 `config/exceptions.py`:`custom_exception_handler`(重寫 `_flatten_message`/`_find_leaf` 為多欄位陣列建構邏輯,並補上狀態碼→預設 code 的對應表)、`handler404`、`handler500`
- 不修改 `apps/accounts/views.py`、`apps/events/views.py`、任何 serializer 的欄位驗證邏輯本身——這次改動集中在錯誤格式化層,不改變驗證規則
