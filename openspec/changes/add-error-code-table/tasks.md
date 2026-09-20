> TDD 排法:RED(先寫會失敗/會被翻案的測試)→ GREEN(只寫剛好讓它通過的最小實作)。這次只有一個 seam:`config/exceptions.py` 的 `custom_exception_handler`/`handler404`/`handler500`,走單元測試(直接呼叫函式,不用起 HTTP server)。
>
> 這個 change 不引入新工具鏈/新依賴,不需要獨立的環境健檢 task。`config/tests/test_exceptions.py` 已存在,這次是在既有檔案上補測試,不是新建檔案。
>
> 有兩個既有測試的期望值會因為這次改動而改變(`test_handler_converts_drf_built_in_not_authenticated` 的 `code` 從 `None` 變成 `"UNAUTHORIZED"`),這是刻意的業務邏輯變更(design.md D3),不是遷就實作結果,依 CLAUDE.md TDD 規範第 5 條連動修改並說明原因。
>
> 每項驗證方式前綴 `(auto)`/`(manual)`:全部是純函式/單元測試,全部可由 `pytest` 自動驗證,沒有需要人工操作確認的步驟。

## 1. Seam: `custom_exception_handler` 的 `errors` 陣列與狀態碼預設 code(對應 spec 需求:統一錯誤回應形狀、沒有業務代碼時 code 依狀態碼決定預設值)

- [x] 1.1 [RED] 在 `config/tests/test_exceptions.py` 補測試與修改既有測試,涵蓋:
  - ① 單一欄位驗證失敗(`ValidationError({"title": ["此欄位必填"]})`)→ `response.data["errors"] == [{"field": "title", "message": "此欄位必填"}]`,頂層 `message` 維持是該欄位訊息(向後相容),`code` 為 `None`
  - ② 多欄位同時驗證失敗(`ValidationError({"title": ["此欄位必填"], "responseDeadline": ["必須晚於現在"]})`)→ `errors` 陣列包含兩筆,兩個 `field` 都要出現,不是只回第一個
  - ③ 巢狀陣列欄位驗證失敗(模擬 DRF 對 `slots`(`many=True` nested serializer)產生的形狀,例如 `ValidationError({"slots": [{}, {"date": ["此為必需欄位。"]}]})`,代表第 0 筆沒問題、第 1 筆的 `date` 有問題)→ `errors` 陣列裡該筆的 `field` 值為 `"slots[1].date"`
  - ④ 兩層巢狀但**不是** list 形狀的既有測試(`test_handler_flattens_two_level_nested_validation_error`,`{"parent": {"child": ["bad"]}}`)維持通過:`field` 是 `"parent"`(不展開成 `"parent.child"`),`message` 透過既有遞迴找 leaf 的邏輯找到 `"bad"`,不誤判成 list 巢狀
  - ⑤ 修改既有 `test_handler_converts_drf_built_in_not_authenticated`:`NotAuthenticated`(401,無 `ApiError` code)→ `response.data["code"]` 從原本斷言 `is None` 改成斷言 `== "UNAUTHORIZED"`,並補上一句說明「這是 design.md D3 的刻意行為變更,不是遷就實作結果」的註解
  - ⑥ 新增:`PermissionDenied`(403,無 `ApiError` code)→ `code == "FORBIDDEN"`
  - ⑦ 新增:DRF `NotFound`(404,無 `ApiError` code)→ `code == "NOT_FOUND"`
  - ⑧ 新增:`ApiError` 明確指定 code 時(例如既有的 `test_handler_converts_api_error_to_message_code_shape`)不受狀態碼查表影響,`code` 仍是 `ApiError` 指定的值——確認既有測試不用改也會通過,順便補一個 401 狀態碼的 `ApiError` 明確 code 案例(例如 `code="INVALID_REFRESH_TOKEN"`),驗證不會被 D3 的查表蓋掉
  - ⑨ 新增:`Gone`(410,無業務 code)→ `code` 維持 `None`(410 不在 D3 的查表範圍內,既有 `test_handler_converts_gone_with_code_none` 不用改,補一句確認 410 不受這次查表影響即可)

  修改後,①②③⑤⑥⑦這幾條應該是 FAIL(`errors` 欄位還不存在、401/403/404 的 code 還是 `None`)— (auto) `pytest config/tests/test_exceptions.py` 顯示這幾條 FAIL

- [x] 1.2 [GREEN] 實作:
  - `config/exceptions.py` 新增一個把 `response.data`(dict 形狀,非 `"detail"` 鍵)展開成 `errors` 陣列的函式,取代 `_flatten_message`/`_find_leaf` 目前「只取第一個欄位」的邏輯:遍歷全部 key;若某個 key 的值是 list 且其中有非空 dict 元素(巢狀陣列欄位的形狀),依序展開成 `"<key>[<index>].<subfield>"`;否則沿用既有遞迴找 leaf 的邏輯,取該欄位第一則訊息。頂層 `message` 取全部 `errors` 裡的第一筆訊息(維持向後相容)
  - 新增一個狀態碼→預設 code 的常數字典(`{400: None, 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 500: "SERVER_ERROR"}`),只在 `code`(非 `ApiError` 指定,即目前邏輯裡 `code = None` 的分支)時查表補上
  - `handler404`/`handler500` 分別把 `"code": None` 改成 `"code": "NOT_FOUND"`/`"code": "SERVER_ERROR"`
  - 讓 1.1 全部轉綠為止,不多加東西——滿足需求「統一錯誤回應形狀」「沒有業務代碼時,code 依狀態碼決定預設值」「未匹配任何路由的請求也符合統一格式」— (auto) `pytest config/tests/test_exceptions.py` 該檔全綠

## 2. 收尾

- [x] 2.1 跑 `uv run ruff check .`、`uv run python manage.py check`、`uv run pytest`(全套,含 `apps/accounts`/`apps/events` 既有測試,確認這次改動沒有連帶弄壞既有的 `ApiError`/`Gone`/401/403/404 相關斷言)— (auto) 三個指令 exit code 皆 0
- [x] 2.2 驗證邊界需求「不改變驗證規則本身、不改變 `apps.accounts` 既有業務 code」:`git diff --stat develop...HEAD` 確認只有 `config/exceptions.py`、`config/tests/test_exceptions.py`、`openspec/changes/add-error-code-table/` 被異動,沒有動到 `apps/accounts/`、`apps/events/` 任何 view 或 serializer 檔案 — (auto) `git diff --stat` 顯示的檔案清單與異動內容不含上述項目
