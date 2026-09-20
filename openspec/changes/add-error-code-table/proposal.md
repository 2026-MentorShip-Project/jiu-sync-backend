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
- `events`:`GET /api/events/{id}` 查無資料的 404 改用活動專屬 code(`EVENT_NOT_FOUND`);新增連結失效判斷,`displayStatus` 算出 `link_expired` 時回 410(`LINK_EXPIRED`)而非 200;`PATCH /api/events/{id}` 對非 active 狀態活動的拒絕從 400 改成 409(`EVENT_NOT_ACTIVE`)

> **修訂記錄(2026-09-20)**:前端進一步確認需求後,推翻了本文件最初「400 不配 code」的決定,以及既有的「新情況一律用通用 code」原則裡「非擁有者以外」的部分。最終定案(取代上面 Why/What Changes 段落裡跟本次修訂矛盾的敘述):
> 1. **400 驗證錯誤恢復配專屬 code**:`errors[]` 每筆新增 `code` 欄位,涵蓋 `apps.accounts`(`idToken` 必填)與 `apps.events`(`POST`/`PATCH` 全部欄位驗證規則,例如 `TITLE_TOO_LONG`、`TOO_MANY_SLOTS`、`DEADLINE_IN_PAST`、`HOST_EMAIL_INVALID` 等)。頂層 `message`/`code` 比照既有「取第一筆」邏輯,同步從第一筆 errors 帶出 `code`。
> 2. **`PATCH` 非擁有者的 403 維持通用 `FORBIDDEN`**,不特別配專屬 code(跟 `apps.accounts` 既有的 `REFRESH_TOKEN_NOT_YOURS` 這種有明確業務語意的既有 code 不同,這是新情境,維持粗粒度)。
> 3. **`PATCH` 非 active 狀態活動的拒絕改成 409 Conflict**(不是 400),配 `EVENT_NOT_ACTIVE`——語意上這是「請求與資源目前狀態衝突」,409 比 400(欄位驗證失敗)更貼切。
> 4. **`GET /api/events/{id}` 新增 410 邏輯**:即使目前系統沒有 finalize/cancel 端點、這個分支測不到真實觸發路徑,仍決定現在就接上(直接建立測試資料驗證),不等 finalize/cancel 端點做出來才一起做。
>
> 這份修訂記錄取代原本「未涵蓋」清單裡「不新增任何欄位驗證規則專屬的 code」「不改變 410 既有行為」兩項排除——這兩項現在都是本次 change 的範圍。

## Impact

- 修改 `config/exceptions.py`:`custom_exception_handler`(重寫 `_flatten_message`/`_find_leaf` 為多欄位陣列建構邏輯,並補上狀態碼→預設 code 的對應表,新增 `FIELD_CODE_OVERRIDES`/`NESTED_SUBFIELD_CODE_OVERRIDES` 把 DRF 內建欄位驗證的通用 code 換成語意化字串)、`handler404`、`handler500`
- 修改 `apps/accounts/views.py`(無,idToken 必填的 code 完全靠 `config/exceptions.py` 的對照表補上,不用動 serializer)
- 修改 `apps/events/serializers.py`(手動 raise 的驗證規則補上明確 `code=`)、`apps/events/views.py`(`GET`/`PATCH /api/events/{id}` 改用 `ApiError`/`Gone` 明確指定 code 與狀態碼,不再用 `get_object_or_404`/泛用 `ValidationError`)
