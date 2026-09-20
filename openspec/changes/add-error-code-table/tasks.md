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

## 2. Seam: 框架內建例外的 message 中文化(對應 spec 需求:框架內建例外的 message 不得夾雜非中文原文,design.md D6)

- [x] 2.1 [RED] 在 `config/tests/test_exceptions.py` 補測試與修改既有測試,涵蓋:`NotAuthenticated`/`PermissionDenied`/`NotFound` 三個既有測試的 `message` 斷言從「非空字串即可」改成精確比對固定中文字串;新增一則模擬 simplejwt `AuthenticationFailed`(帶英文 `.detail`,例如 `"Given token not valid for any token type"`)的測試,確認 `message` 換成固定中文、不含原始英文字串。修改後這幾條應該 FAIL(`message` 還是英文原文)— (auto) `pytest config/tests/test_exceptions.py` 顯示這幾條 FAIL
- [x] 2.2 [GREEN] 在 `config/exceptions.py` 新增 `STATUS_CODE_DEFAULT_MESSAGES` 對照表(401/403/404/500 對應固定中文文案,400 不在表裡),只在 `custom_exception_handler` 的「detail」形狀分支套用(`ValidationError` 的 `errors` 陣列/頂層 `message` 不受影響),讓 2.1 全部轉綠 — (auto) `pytest config/tests/test_exceptions.py` 該檔全綠

## 3. Seam: 欄位驗證 code 對照表 + 活動業務 code(對應 spec 需求:欄位驗證錯誤 SHALL 各自帶有語意化的 code;events capability 的 EVENT_NOT_FOUND/LINK_EXPIRED/EVENT_NOT_ACTIVE,見 design.md D7/D8、修訂記錄)

> 這個 seam 是推翻本 change 最初「400 不配 code」決定後追加的,範圍擴大到 `apps.accounts`/`apps.events` 的 serializer/view,不再是只動 `config/exceptions.py`。

- [x] 3.1 [RED] 補測試,涵蓋:
  - `config/tests/test_exceptions.py`:①②③既有測試(單一/多欄位/巢狀陣列驗證錯誤)補上 `code` 斷言;新增查有對照表(`title`+`max_length` → `TITLE_TOO_LONG`)與查無對照表(fallback 沿用 DRF 原始 code)兩種案例
  - `apps/accounts/tests/test_views.py`:`idToken` 未填 → 400,code 為 `ID_TOKEN_REQUIRED`
  - `apps/events/tests/test_views.py`:`POST /api/events` 的 `title`/`hostNickname`/`slots`(0 筆與超過 20 筆分兩案例)/`responseDeadline` 驗證失敗補上對應 code 斷言;`PATCH` 的 `title`/`responseDeadline`/`hostEmail` 格式錯誤補上對應 code;`GET`/`PATCH` 的 404 補上 `EVENT_NOT_FOUND`;新增 410/`LINK_EXPIRED` 測試(直接建立 `status=cancelled`/`cancelled_at` 超過 7 天的測試資料,不透過任何 API);`PATCH` 非 active 狀態的測試從斷言 400 改成斷言 409 + `EVENT_NOT_ACTIVE`;非擁有者 403 補上斷言 code 仍是通用 `FORBIDDEN`
  — (auto) `pytest` 顯示上述新增/修改的案例 FAIL

- [x] 3.2 [GREEN] 實作:
  - `config/exceptions.py`:`_find_leaf` 改寫成 `_find_leaf_detail`(保留 `ErrorDetail` 物件,不提前轉字串);新增 `FIELD_CODE_OVERRIDES`/`NESTED_SUBFIELD_CODE_OVERRIDES` 對照表與 `_resolve_field_code` 查表函式;`_build_errors` 每筆 `errors` 元素補上 `code`;頂層 `code` 比照 `message` 改成「第一筆的值」
  - `apps/events/serializers.py`:`_validate_host_nickname_weighted_length`/`_validate_response_deadline_in_future`/`validate_slots`(拆成 0 筆與超過 20 筆兩個分支)補上明確 `code=`
  - `apps/events/views.py`:新增模組層級 `_get_event_or_404` 函式(`ApiError(..., code="EVENT_NOT_FOUND", status_code=404)`,取代 `get_object_or_404`);`get()` 算出 `displayStatus == "link_expired"` 時 `raise Gone(..., code="LINK_EXPIRED")`;`patch()` 非 active 狀態改用 `ApiError(..., code="EVENT_NOT_ACTIVE", status_code=409)`(原本是 400 的 `serializers.ValidationError`);`EventListView.get()` 的 `owner=me` 缺參數錯誤補上 `code="OWNER_PARAM_REQUIRED"`
  - 讓 3.1 全部轉綠 — (auto) `pytest` 全套通過

## 4. 收尾

- [x] 4.1 跑 `uv run ruff check .`、`uv run python manage.py check`、`uv run python manage.py makemigrations --check --dry-run`、`uv run pytest`(全套)— (auto) 四個指令 exit code 皆 0
- [x] 4.2 驗證邊界需求「不改變任何驗證規則的判斷邏輯本身、不改變 `apps.accounts` 既有業務 code」:確認 `apps/accounts/views.py`(既有 `ApiError` 呼叫點)、`Event`/`User` model、既有驗證的長度上限/必填判斷邏輯本身都沒有被更動,只有「附帶什麼 code」變了 — (auto) 逐一核對 diff 內容不含既有業務 code 字串變更、不含驗證門檻數字變更
