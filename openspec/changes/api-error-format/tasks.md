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
