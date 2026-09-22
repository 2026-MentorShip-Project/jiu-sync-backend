## Why

前端在對接 `POST /api/events`（與其他端點）的錯誤回應時發現:`code` 目前一律是 `null`,而且 400 驗證錯誤只會回傳**第一個**失敗欄位,無法一次標紅多個錯誤欄位。跟前端來回確認需求後,最終定案:400 驗證錯誤的每個欄位都要各自帶一個語意化的 `code`(不是只有 `field`+`message`),401/403/404/500 這種「同一狀態碼可能有多種原因、且沒有既有業務 code」的情況也要有穩定的預設 code;`GET`/`PATCH /api/events/{id}` 另外需要幾個活動專屬的業務 code(查無資料、連結失效、狀態衝突)。

## What Changes

- `POST`/`PATCH /api/events` 等端點的 400 驗證錯誤回應新增 `errors` 欄位:`[{field, code, message}, ...]`,列出**全部**失敗欄位(不只第一個),每筆各自帶語意化的 `code`(例如 `TITLE_TOO_LONG`、`TOO_MANY_SLOTS`、`DEADLINE_IN_PAST`),涵蓋 `apps.accounts`(`idToken` 必填)與 `apps.events` 的全部欄位驗證規則;巢狀欄位(例如候選時段)用 `slots[0].date` 這種路徑格式命名。頂層 `message`/`code` 是 `errors` 第一筆的值,提供向後相容的簡化視圖
- 401/403/404/500 在沒有既有業務 `ApiError`(例如 `apps.accounts` 已有的 `INVALID_REFRESH_TOKEN`)指定 code 的情況下,自動依狀態碼補上固定 code:`UNAUTHORIZED`/`FORBIDDEN`/`NOT_FOUND`/`SERVER_ERROR`;`ApiError` 就算有指定 `status_code` 但沒指定 `code`,也套用同一份預設值,不會因為用了 `ApiError` 就整支繞過查表
- Django URL 層級的 404(`handler404`,走不到任何 view)與未攔截的 500(`handler500`)也補上 `NOT_FOUND`/`SERVER_ERROR`,不再固定回 `null`
- `GET /api/events/{id}` 查無資料的 404 改用活動專屬 code(`EVENT_NOT_FOUND`);新增連結失效判斷,`displayStatus` 算出 `link_expired` 時回 410(`LINK_EXPIRED`)而非 200
- `PATCH /api/events/{id}` 對非 active 狀態活動的拒絕從 400 改成 409(`EVENT_NOT_ACTIVE`);非擁有者的 403 維持通用 `FORBIDDEN`,不特別配專屬 code(跟 `apps.accounts` 既有的 `REFRESH_TOKEN_NOT_YOURS` 這種有明確業務語意的既有 code 不同,這是新情境,刻意維持粗粒度)

未涵蓋(明確排除):不改變 `apps.accounts` 現有的業務 code(`INVALID_ID_TOKEN`/`EMAIL_ALREADY_IN_USE`/`INVALID_REFRESH_TOKEN`/`REFRESH_TOKEN_NOT_YOURS`);不改變任何驗證規則的判斷邏輯本身(長度上限、必填與否等)——這次改動只在「附帶什麼 code」,不改「什麼情況算失敗」。

## Capabilities

### New Capabilities

(無)

### Modified Capabilities

- `api-error-format`:統一錯誤回應形狀的 `errors` 陣列每筆補上 `code`;401/403/404/500 無業務 code 時的預設行為從「固定 null」改為「依狀態碼補上固定 code」,含 `ApiError` 未指定 code 的情況
- `events`:`GET /api/events/{id}` 查無資料的 404 改用活動專屬 code(`EVENT_NOT_FOUND`);新增連結失效判斷,`displayStatus` 算出 `link_expired` 時回 410(`LINK_EXPIRED`)而非 200;`PATCH /api/events/{id}` 對非 active 狀態活動的拒絕從 400 改成 409(`EVENT_NOT_ACTIVE`)

> **決策背景(2026-09-20 修訂)**:本 change 最初的方向是「400 不配 code,靠 `field`+`message` 就夠」,且「未涵蓋」清單原本包含「不新增任何欄位驗證規則專屬的 code」「不改變 410 既有行為」。前端進一步確認需求後推翻了這些決定,也推翻了「PATCH 非擁有者以外的新情況一律用通用 code」原則裡的部分適用範圍——上面的 Why/What Changes 已經是推翻後的最終版本,這裡只留背景,不再重複列出取代前後的差異。

## Impact

- 修改 `config/exceptions.py`:`custom_exception_handler`(重寫 `_flatten_message`/`_find_leaf` 為多欄位陣列建構邏輯,並補上狀態碼→預設 code 的對應表,新增 `FIELD_CODE_OVERRIDES`/`NESTED_SUBFIELD_CODE_OVERRIDES` 把 DRF 內建欄位驗證的通用 code 換成語意化字串)、`handler404`、`handler500`
- 修改 `apps/accounts/views.py`(無,idToken 必填的 code 完全靠 `config/exceptions.py` 的對照表補上,不用動 serializer)
- 修改 `apps/events/serializers.py`(手動 raise 的驗證規則補上明確 `code=`)、`apps/events/views.py`(`GET`/`PATCH /api/events/{id}` 改用 `ApiError`/`Gone` 明確指定 code 與狀態碼,不再用 `get_object_or_404`/泛用 `ValidationError`)
