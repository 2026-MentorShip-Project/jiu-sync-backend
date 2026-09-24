> TDD 排法:RED(先寫會失敗的測試)→ GREEN(只寫剛好讓它通過的最小實作)。Seam 走單元測試(直接呼叫 `custom_exception_handler`/`handler500`,不用起 HTTP server),用 pytest 的 `caplog` fixture 斷言 log 內容。
>
> 這個 change 不引入新工具鏈/新依賴,不需要獨立的環境健檢 task。`config/tests/test_exceptions.py` 已存在,這次是在既有檔案上補測試,不是新建檔案。
>
> 每項驗證方式前綴 `(auto)`/`(manual)`:全部是純函式/單元測試,全部可由 `pytest` 自動驗證,沒有需要人工操作確認的步驟。

## 1. Seam: `custom_exception_handler`/`handler500` 補上後端查驗用 log(對應 spec 需求:401/403/5xx 錯誤回應 SHALL 記錄後端可查驗的 log)

- [x] 1.1 [RED] 在 `config/tests/test_exceptions.py` 補測試,涵蓋:
  - ① 401(帶 `context["request"]`,含已登入使用者)→ 記一筆 WARNING,內容含狀態碼/`code`/使用者 id/請求路徑
  - ② 403(未帶 user)→ 記一筆 WARNING,不因為匿名請求而噴例外
  - ③ 明確 `ApiError(..., status_code=500)`(業務程式碼直接拋 500)→ 記一筆 ERROR(不是 WARNING)
  - ④ 400/404/409(`@pytest.mark.parametrize`)→ 不記 log
  - ⑤ 410(`Gone`)→ 不記 log
  - ⑥ `context` 沒有 `"request"`(既有測試多半直接傳 `{}`)→ 仍正常記 log,不噴例外
  - ⑦ `handler500`(在 `except` 區塊內呼叫,模擬 Django 真實呼叫時機)→ 記一筆 ERROR,`record.exc_info` 不為 `None`

  確認這組測試現在是 FAIL(對應的 log 呼叫還不存在)— (auto) `pytest config/tests/test_exceptions.py` 顯示 FAIL

- [x] 1.2 [GREEN] 實作:`config/exceptions.py` 新增 `logger = logging.getLogger(__name__)`、`_LOGGED_STATUS_CODES = {401, 403}`、`_log_error_response(exc, context, status_code, code, message)`(401/403 用 `logger.warning`,5xx 用 `logger.error` 並帶 `exc_info`,其餘 return 不記;`context.get("request")` 與 `request.user` 皆用 `getattr(..., None)` 保護);`custom_exception_handler` 在組好 `response.data` 之後、`return response` 之前呼叫;`handler500` 開頭呼叫 `logger.error(..., exc_info=True)`。讓 1.1 全部轉綠為止 — (auto) `pytest config/tests/test_exceptions.py` 該檔全綠

## 2. 收尾

- [x] 2.1 全套驗證:`pytest -q`(194 passed)、`ruff check .`、`manage.py check` 皆乾淨 — (auto)
- [x] 2.2 `/code-review`:跑一次自審,無發現真實問題 — (auto)
