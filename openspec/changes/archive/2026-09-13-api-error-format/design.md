## Context

目前 `REST_FRAMEWORK` 設定（`config/settings/base.py`）沒有自訂 `EXCEPTION_HANDLER`，走 DRF 預設行為：不同例外類型回傳的 body 形狀不一致（`{"detail": "..."}`、巢狀欄位錯誤字典等）。目前沒有任何 view 實作完成（`google-sso-login` 的 view 還在進行中），這是在任何 view 依賴特定錯誤形狀之前，先把這層基礎設施定下來。見 proposal.md - Why。

## Goals / Non-Goals

**Goals:**
- 讓「view 拋出的例外」統一轉成 `{message, code}` 這個 body 形狀
- 讓 view 層有一個明確的方式可以指定 `code`（給前端做程式化分支用）
- 補上 DRF 沒有的 410 Gone

**Non-Goals:**
- 不處理未被辨識的伺服器端例外（例如程式本身的 bug 拋出的一般 `Exception`）——那些應該繼續照 Django 預設行為回 500、留在錯誤紀錄裡看得到，不要被這層悄悄吞掉或包裝成看起來像「正常的業務錯誤」
- 不要求把 DRF `ValidationError` 那種巢狀欄位錯誤字典完整保留結構——這個 change 只保證有一個可讀的 `message` 字串，欄位層級的細節不是這次的重點（大部分需要精確錯誤碼的 400，Swagger 文件裡都是 view 自己指定 `code`，不是依賴 DRF 自動產生的欄位驗證錯誤）

## Decisions

**共用模組放 `config/exceptions.py`，不是某個 app 底下。**
這是跨越整個 API 的基礎設施（events、留言板、AI 推薦之後都會用到），放在 `config/`（專案層級）而不是 `apps/accounts/` 之類的特定 app，避免之後其他 app 要用還得跨 app import。

**`ApiError(APIException)`：view 層要帶 `code` 時用這個拋。**
```python
class ApiError(APIException):
    def __init__(self, message, code=None, status_code=None):
        ...
```
`message` 進 DRF 的 `.detail`，`code` 存成 `.api_code`（避開 DRF 例外類別本來就有的 `.default_code`／`get_codes()` 這組跟欄位驗證相關的既有機制，不要混用同一個名字，減少誤用機會）。`status_code` 可選填，預設沿用 `APIException.status_code`（500）——但實務上 view 幾乎都會指定明確的狀態碼（400/403/404/410）。

**`Gone(APIException)`：`status_code = 410`。**
DRF 內建例外沒有 410，這裡補一個最小的子類別，用法跟 `ApiError` 一樣但狀態碼固定。

**`custom_exception_handler(exc, context)` 的轉換規則：**
1. 先呼叫 DRF 預設的 `rest_framework.views.exception_handler(exc, context)`。這一步的價值：它已經把 Django 的 `Http404`／`PermissionDenied` 自動轉成 DRF 的 `NotFound`／`PermissionDenied`，不用自己重寫這段轉換。
2. 如果預設 handler 回傳 `None`（代表這個例外不是 DRF/Django 認得的類型，例如程式本身的 bug）——直接回傳 `None`，讓 Django 走它原本 500 的路，不要在這裡插手（見 Non-Goals）。
3. 如果 `exc` 是 `ApiError`（或 `Gone`）：`message = exc.detail`，`code = getattr(exc, "api_code", None)`。
4. 其他情況（DRF 內建例外，例如 `NotAuthenticated`、`PermissionDenied`、`NotFound`、`ValidationError`、`MethodNotAllowed`）：`code = None`；`message` 這樣取：
   - `response.data` 是 dict 且有 `"detail"` 鍵 → 用它
   - `response.data` 是字串 →直接用
   - 其他情況（例如 `ValidationError` 的巢狀欄位字典）→ 取第一個欄位的第一條錯誤訊息，格式化成 `"<field>: <error>"` 這樣一句話（對應 Non-Goals 提到的取捨：不保留完整結構，只保證有一句可讀訊息）
5. 把 `response.data` 整個換成 `{"message": message, "code": code}`，其餘（`status_code` 等）維持 DRF 預設 handler 已經算好的值。

## Post-review 補充決策（2026-09-13）

實作完成後的 code review 發現：`custom_exception_handler` 只在請求真的進到某個 DRF view 才會被呼叫；一個完全不匹配任何 URL 的請求（例如打錯路徑），Django 的 URL resolver 在進到任何 view 之前就直接回它自己的 HTML 404 頁——這是 spec 寫的「所有非 2xx」跟實作之間一個真實的落差，不是誤報。已跟使用者確認：補 Django 層級的 `handler404`／`handler500`，而不是把 spec 收窄成「只保證 DRF view 內」。

**`handler404`／`handler500` 是 Django 的機制，不是 DRF 的。**
在 `config/urls.py` 模組層級指定 `handler404 = "config.exceptions.handler404"`、`handler500 = "config.exceptions.handler500"`（Django 官方指定方式，必須是 URLconf 模組層級的名稱，不能塞進 `REST_FRAMEWORK` settings）。這兩個 view function 回傳跟 `custom_exception_handler` 一致的 `{"message": ..., "code": null}` 形狀（`handler404` 用固定訊息如「找不到這個路徑」；`handler500` 用固定的通用訊息，不能包含任何例外細節——見下方澄清）。

**這不違反原本的 Non-Goal（不吞真正的伺服器錯誤）。**
`handler500` 只改變「回給使用者的 body 長什麼樣子」，Django 自己的錯誤紀錄機制（`django.request` logger、`DEBUG=False` 時的錯誤通知）完全不受影響、照常運作——伺服器端看得到的資訊沒有變少，只是使用者不再看到一頁 HTML debug 頁或通用錯誤頁，而是跟其他 API 錯誤一致的 JSON。這跟 `custom_exception_handler` 本身「不吞不認得的 DRF 例外」的原則是兩件獨立的事：DRF 層級的例外處理邏輯完全沒變，這裡只是在 Django 最外層再加一層安全網。

## Post-review 補充決策：巢狀驗證錯誤攤平（低優先度，一併處理）

`_flatten_message` 原本只處理第一層 `list`，遇到巢狀 serializer 錯誤（例如 `{"child": ["bad"]}`）會產生 `field: {'child': ['bad']}` 這種還算是字串、但不算「一句可讀訊息」的結果。改成遞迴找第一個字串 leaf：如果值是 `dict` 或 `list`，繼續往下一層找，直到找到字串為止。

## Risks / Trade-offs

- **[ValidationError 攤平成單一訊息，遺失欄位層級細節]** → 已知取捨（見 Non-Goals）。真的需要精確錯誤碼的情境，Swagger 文件顯示都是 view 自己手動拋 `ApiError` 指定 `code`（例如 `EVENT_CANCELLED`），不依賴這裡的自動攤平邏輯；如果之後發現前端真的需要逐欄位的驗證錯誤，屬於新的需求，屆時再開一次 change 處理，不在這次範圍內預先設計。
- **[未辨識例外被刻意放行、不轉格式]** → 這是刻意的行為（Non-Goals 已說明），但代表「非預期的 500」的 body 形狀跟這裡定義的 `{message, code}` 不一致——如果前端假設所有非 2xx 都符合這個形狀，遇到真正的伺服器錯誤仍要有 fallback 處理，不能假設一定拿得到 `code` 欄位。
