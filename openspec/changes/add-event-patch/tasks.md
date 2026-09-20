> TDD 排法:RED(先寫會失敗的測試)→ GREEN(只寫剛好讓它通過的最小實作)。這次只有一個 seam:`PATCH /api/events/{id}`,走 HTTP 層測(DRF test client),不額外拆子 seam。
>
> 這個 change 不引入新工具鏈/新依賴(沿用既有 Django/DRF/JWT/`config/exceptions.py`),不需要獨立的環境健檢 task。六個可改欄位已存在於 `Event` model(`add-events-api` 已建立),不需要 migration,不需要 section 1 的 model 前置。
>
> 每項驗證方式前綴 `(auto)`/`(manual)`:全部是後端 API,全部可由 `pytest`/`ruff`/`manage.py` 指令自動驗證,沒有需要人工操作確認的步驟。

## 1. Seam: `PATCH /api/events/{id}`(對應 spec 需求:主揪編輯活動基本資訊)

- [x] 1.1 [RED] 在 `apps/events/tests/test_views.py` 補測試,涵蓋:① 擁有者僅送單一欄位(如 `title`)→ 200,該欄位更新、其餘五欄位維持原值,回應格式與 `GET /api/events/{id}` 相同(含 `slots`/`displayStatus`/`isOwner` 等完整欄位);② 擁有者一次送六個欄位全部更新 → 200,六欄位皆正確更新並反映在回應中;③ 空 body `{}` → 200,不更動任何欄位,回傳目前狀態;④ body 帶 `mode`(六欄位外的欄位)→ 200,`Event.mode` 不受影響,忽略該欄位;⑤ 已登入但非該活動擁有者送出編輯 → 403,資料庫該筆活動完全未變動;⑥ 未登入(不帶 token)→ 401,資料庫未變動;⑦ 活動 `status="finalized"` 時編輯 → 400,資料庫未變動;活動 `status="cancelled"` 時編輯 → 400,資料庫未變動(用 `Event.objects.create(..., status=...)` 直接建立測試資料,不透過任何 API 產生這兩種狀態);⑧ `responseDeadline` 等於或早於送出當下時間 → 400,資料庫未變動;⑨ `hostEmail` 改成與 `request.user.email` 不同、但格式合法的另一個 Email → 200,`Event.host_email` 更新為請求中的新值(確認脫鉤);⑩ 對不存在的活動 id 送出編輯 → 404,body 符合 `api-error-format` 的 `{message, code}` 形狀;⑪ `title` 超過 30 字元 → 400(確認沿用既有長度驗證邏輯有正確接上)。確認這組測試現在是 FAIL(view 還沒有 `patch()` 方法,或 method not allowed)— (auto) `pytest` 顯示這幾條 FAIL
- [x] 1.2 [GREEN] 實作:
  - `apps/events/serializers.py` 新增 `EventPatchSerializer`(`ModelSerializer`,六個欄位皆 `required=False`;`hostNickname`/`responseDeadline` 沿用既有 `source=` camelCase 映射;`hostEmail` 宣告為一般 `EmailField(required=False)`,不再像 `EventCreateSerializer` 那樣拒絕採信;重用既有 `_weighted_length` 函式做 `hostNickname` 驗證;`responseDeadline` 驗證須晚於 `timezone.now()`)
  - `apps/events/views.py`:`EventDetailView` 覆寫 `get_permissions()`/`get_authenticators()`,依 `self.request.method` 分派——`GET` 維持現有 `AllowAny` + `OptionalJWTAuthentication`,`PATCH` 回傳 `[IsAuthenticated()]` + 全域預設 `JWTAuthentication`(不用 optional 版本);新增 `patch()` 方法:先 `get_object_or_404` 取出活動,檢查 `request.user == event.owner`(否則 403,DRF `PermissionDenied`),檢查 `event.status == Event.Status.ACTIVE`(否則 400,`ValidationError`),通過後用 `EventPatchSerializer(event, data=request.data, partial=True)` 驗證並儲存,成功回傳 `EventDetailSerializer(event, context={"request": request}).data`
  - 讓 1.1 全部轉綠為止,不多加東西——滿足需求「主揪編輯活動基本資訊」 — (auto) `pytest apps/events/tests/test_views.py` 該檔全綠

## 2. 收尾

- [x] 2.1 跑 `uv run ruff check .`、`uv run python manage.py check`、`uv run pytest`(全套),確認三者皆乾淨無誤 — (auto) 三個指令 exit code 皆 0
- [x] 2.2 驗證邊界需求「未涵蓋範圍不得被誤實作」:確認沒有新增 finalize/cancel 端點、`/live`、`ParticipantResponse`、任何允許編輯 `mode`/`slots` 的程式碼路徑 — (auto) `git diff --stat feature/events-api-crud...HEAD -- apps/events` 顯示的檔案清單與異動內容不含上述項目
