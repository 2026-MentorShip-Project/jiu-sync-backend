> TDD 排法同 `google-sso-login`：先寫會失敗的測試（RED）→ 只寫剛好讓它通過的最小實作（GREEN），垂直切。這個 change 不引入新工具鏈／新依賴（DRF 已經裝好），不需要環境健檢 task。全部可由 `pytest`／`ruff`／`manage.py` 自動驗證，沒有 `(manual)` 步驟。

## 1. Seam: `ApiError` / `Gone` 例外類別

- [x] 1.1 [RED] 在 `config/tests/test_exceptions.py` 寫測試：① `ApiError("訊息", code="SOME_CODE", status_code=400)` 建立後 `.detail`（或轉字串）等於 `"訊息"`、`.api_code == "SOME_CODE"`、`.status_code == 400`；② `ApiError("訊息")` 不帶 `code`/`status_code` 時 `.api_code is None`、`.status_code` 為預設值；③ `Gone("連結已失效")` 的 `.status_code == 410`。確認這組測試先是 FAIL（`config/exceptions.py` 還不存在）——滿足需求「View 可以指定機器可讀的錯誤代碼」 — (auto) `pytest` 顯示 FAIL
- [x] 1.2 [GREEN] 在 `config/exceptions.py` 實作 `ApiError(APIException)`、`Gone(APIException, status_code=410)`，只寫到讓 1.1 轉綠 — (auto) `pytest` 該檔全綠

## 2. Seam: `custom_exception_handler`

- [x] 2.1 [RED] 滿足需求「統一錯誤回應形狀」「沒有機器可讀代碼時 code 明確為 null」「連結失效與資源不存在的狀態碼分離」：在 `config/tests/test_exceptions.py` 繼續加測試，透過 `rest_framework.views.APIView().handle_exception(exc)`（DRF 真正的例外處理入口，會照 settings 設定呼叫 `EXCEPTION_HANDLER`——**要先完成 2.2 才把 `EXCEPTION_HANDLER` 接上 settings，這裡先直接測 handler 函式本身，或用 `@override_settings` 局部指定，兩種方式擇一，由實作時判斷哪種在這個專案的測試環境下更乾淨**）驗證：
  ① 拋 `ApiError("找不到這筆資料", code="NOT_FOUND_X", status_code=404)` → response `.status_code == 404`，`.data == {"message": "找不到這筆資料", "code": "NOT_FOUND_X"}`
  ② 拋 `Gone("連結已失效")` → `.status_code == 410`，`.data == {"message": "連結已失效", "code": None}`（滿足需求「連結失效與資源不存在的狀態碼分離」）
  ③ 拋 DRF 內建 `rest_framework.exceptions.NotAuthenticated()` → `.data` 是 `{"message": ..., "code": None}` 形狀，不是 DRF 預設的 `{"detail": ...}`（滿足需求「統一錯誤回應形狀」「沒有機器可讀代碼時 code 明確為 null」）
  ④ 拋 DRF 內建 `rest_framework.exceptions.ValidationError({"title": ["此欄位必填"]})` → `.data["code"] is None`，`.data["message"]` 是一句可讀字串（不是巢狀 dict）
  ⑤ 拋一個沒被 DRF/Django 認得的一般 `Exception("boom")` → handler 回傳 `None`（不吞掉、不包裝成看起來像業務錯誤——滿足 design.md Non-Goals）
  確認這組測試現在是 FAIL——滿足需求「統一錯誤回應形狀」「沒有機器可讀代碼時 code 明確為 null」「連結失效與資源不存在的狀態碼分離」 — (auto) `pytest` 顯示 FAIL
- [x] 2.2 [GREEN] 在 `config/exceptions.py` 實作 `custom_exception_handler(exc, context)`：先呼叫 DRF 預設 `exception_handler`，`None` 就直接回傳；`ApiError`/`Gone` 走 `.detail`/`.api_code`；其他情況照 design.md 的規則從 `response.data` 抽出 `message`、`code` 固定 `None`。只寫到讓 2.1 轉綠 — (auto) `pytest` 該檔全綠

## 3. 接上設定並收尾

- [x] 3.1 在 `config/settings/base.py` 的 `REST_FRAMEWORK` 加上 `"EXCEPTION_HANDLER": "config.exceptions.custom_exception_handler"` — (auto) `python manage.py check` 無錯
- [x] 3.2 跑 `uv run pytest`（全專案）、`uv run ruff check .`、`uv run python manage.py check`，三者皆需乾淨 — (auto) 三個指令 exit code 皆 0

## 4. Post-review 修正：`handler404`／`handler500`（對應需求「未匹配任何路由的請求也符合統一格式」）

- [x] 4.1 [RED] 在 `config/tests/test_exceptions.py` 寫測試：用 Django test `Client()`（不是 DRF 的 `APIClient`，因為要測的是 URL resolver 層級，不透過任何 DRF view）打一個不存在的路徑 → ① 狀態碼 404 ② `response.json() == {"message": ..., "code": None}`（`message` 只要是非空字串即可，不檢查精確文字）。確認先是 FAIL（`handler404` 還不存在，此刻應該還是拿到 HTML）——滿足需求「未匹配任何路由的請求也符合統一格式」 — (auto) `pytest` 顯示 FAIL
- [x] 4.2 [GREEN] 在 `config/exceptions.py` 實作 `handler404(request, exception)`、`handler500(request)`（Django 的 handler 簽名規定如此，`handler500` 沒有 `exception` 參數），各自回傳 `JsonResponse({"message": ..., "code": None}, status=404/500)`；在 `config/urls.py` 模組層級加上 `handler404 = "config.exceptions.handler404"`、`handler500 = "config.exceptions.handler500"`。只寫到讓 4.1 轉綠 — (auto) `pytest` 該檔全綠
- [x] 4.3 手動確認 `handler500` 沒有把例外細節洩漏進回應（`message` 是寫死的固定字串，不是 `str(exc)`），也沒有影響 Django 自己的錯誤紀錄機制（不用寫測試驗證日誌，只要程式碼本身沒有攔截/覆蓋 `django.request` logger 或任何錯誤通知機制即可，用 code review 確認） — (auto) 檢視 `config/exceptions.py` 的 diff，確認沒有動到任何 logging 相關設定

## 5. Post-review 修正：巢狀驗證錯誤攤平

- [x] 5.1 [RED] 在 `config/tests/test_exceptions.py` 寫測試：拋 `rest_framework.exceptions.ValidationError({"parent": {"child": ["bad"]}})`（巢狀兩層），驗證 `custom_exception_handler` 轉出來的 `message` 是一句純字串（不含 `{`/`}` 這種 dict repr 痕跡），且包含 `"bad"` 這個實際的 leaf 錯誤內容。確認先是 FAIL（目前的 `_flatten_message` 只攤平第一層，會產生 `parent: {'child': ['bad']}` 這種還帶 dict repr 的字串）— (auto) `pytest` 顯示 FAIL
- [x] 5.2 [GREEN] 把 `config/exceptions.py` 的 `_flatten_message` 改成遞迴：值是 `dict`/`list` 就繼續往下一層找，直到找到字串 leaf 為止，才組成 `"<field>: <leaf>"`。只寫到讓 5.1 轉綠 — (auto) `pytest` 該檔全綠

## 6. 收尾

- [x] 6.1 跑 `uv run pytest`（全專案）、`uv run ruff check .`、`uv run python manage.py check`，三者皆需乾淨 — (auto) 三個指令 exit code 皆 0
