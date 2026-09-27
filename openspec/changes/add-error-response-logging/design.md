## Context

使用者原始需求(逐字):「除了給前端的 error code 之外,請在 raise 之處加上後端可以查驗的 log 資料」。字面上聽起來像要逐一去改全站每個 `raise` 語句,但全站 32 處 `raise ApiError`/`raise serializers.ValidationError`(不含測試)最終都已經流經 `config/exceptions.py::custom_exception_handler` 這個唯一出口(見該檔案 docstring 與既有測試 `config/tests/test_exceptions.py`)。

## Decisions

### D1. 集中在 `custom_exception_handler`/`handler500` 加 log,不逐一碰 32 個 raise 呼叫點

跟使用者確認過(grill-me):32 處 raise 全部流經同一個 handler,集中加一行:
- 自動涵蓋現有全部 32 處,也涵蓋未來新增的 raise,不會有「忘記加」的風險
- 不用在每個呼叫點重複寫幾乎一樣的 log 語句
- 唯一取捨:無法在集中點拿到呼叫端當下才有的區域變數(例如是哪個 `slotId`)。但 `message`(業務訊息文字,通常已經包含關鍵資訊)、請求路徑、`code`、使用者 id 已經是排查問題時最常用的脈絡,足夠應付「後端可查驗」這個目的,不需要為了少數情境犧牲全站一致、零遺漏的優點

若未來真的需要更細的欄位層級脈絡,屬於獨立的後續 change,不在本次 scope。

### D2. 只記 401/403 + 5xx,不記 400/404/409/410

跟使用者確認過(grill-me):
- 400(欄位驗證失敗)、404(資料找不到)、409(狀態衝突)、410(連結失效)是正常業務流程的一部分,使用者打錯格式、分享過期連結都會觸發,量大且多半沒有排查價值,記了只是雜訊
- 401/403 可能代表憑證被冒用或竄改,值得留意,記 WARNING
- 5xx 是伺服器真的出包,一定要記,且用 ERROR 等級

`_LOGGED_STATUS_CODES = {401, 403}`,再另外判斷 `status_code >= 500`,兩者皆不成立就直接 return,不記。

### D3. `handler500` 額外處理(不能只靠 `custom_exception_handler`)

`custom_exception_handler` 只有在 `drf_exception_handler(exc, context)` 認得該例外類型(`APIException` 子類、`Http404`、`PermissionDenied` 等)時才會被呼叫、才回傳非 `None`。一般未預期的例外(程式本身的 bug,例如 `AttributeError`/`KeyError`)不是 `APIException`,`drf_exception_handler` 回 `None`,會往上炸穿到 Django 的 `handler500`——這支處理的正是「真正未預期的例外」,是最該留存 traceback 的情境,所以額外在這裡也加 log,用 `exc_info=True`(Django 呼叫 `handler500` 時仍在例外處理的 except 區塊內,`sys.exc_info()` 有效)。

### D4. 不新增 `LOGGING` settings

專案目前完全沒有 `LOGGING` 設定。Python logging 在沒有任何 handler 設定時,`logging.lastResort`(WARNING 等級起、輸出到 stderr 的內建 fallback handler)仍會正常運作,所以這次的 `logger.warning`/`logger.error` 呼叫不需要額外設定就能在 `docker compose logs`/終端機看到。格式化(時間戳記、結構化欄位)與集中收集(送到外部 log 服務)是後續 infra 議題,不在本次 scope 內。

## Risks / Trade-offs

- **[風險] 5xx 的 `exc_info` 可能包含敏感資訊(例如 SQL 參數)進 log** → 這是所有後端系統記錄未預期例外的通用取捨,log 本身預設只有後端維運人員可查驗,不對外曝露(回給前端的 body 完全不受影響,仍是固定的「伺服器發生未預期的錯誤」)。非本次新增風險,現有 `apps/notifications/tasks.py` 的 `_schedule_notification` 已有先例(`logger.exception`)。

## Migration Plan

不需要 migration——純新增 log 呼叫,不改變任何 API 回應內容或資料庫結構。
