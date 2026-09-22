> TDD 排法：RED（先寫會失敗的測試）→ GREEN（只寫剛好讓它通過的最小實作）。Seam 走 HTTP 層測（DRF test client）。
>
> 這個 change 不引入新工具鏈／新依賴（沿用既有 Django/DRF），需要一個新 migration（新 model），這是既有工具鏈下的新 schema，不算「新工具鏈或新依賴」，不需要獨立的環境健檢 task，直接併入 Seam 1。
>
> 每項驗證方式前綴 `(auto)`/`(manual)`：全部是後端 API，全部可由 `pytest`/`ruff`/`manage.py` 指令自動驗證，沒有需要人工操作確認的步驟。

## 1. Seam: `POST`/`GET /api/events/{id}/comments`（對應 spec 需求：新增活動留言、查詢活動留言列表）

- [x] 1.1 [RED] 新增 `Comment` model（`apps/events/models.py`）：`id`（CharField，primary_key、max_length=8、`default=generate_short_id`、`editable=False`，重用既有 `apps/events/ids.generate_short_id`）、FK `Event`（`related_name="comments"`）、`nickname`（CharField max_length=40）、`message`（CharField max_length=200）、`created_at`（`auto_now_add=True`）。跑 `makemigrations` 產生 migration。在 `apps/events/tests/test_views.py` 補測試，涵蓋：
  - `POST`：① 合法暱稱＋內容 → 201，回應含 `id`（8 碼 base62 格式）／`nickname`／`message`／`createdAt`，DB 有一筆對應資料；② 暱稱前後帶空白，trim 後儲存；③ 暱稱缺漏 → 400 `NICKNAME_REQUIRED`；④ 內容缺漏 → 400 `MESSAGE_REQUIRED`；⑤ 內容超過 200 字 → 400 `MESSAGE_TOO_LONG`；⑥ 活動 `status` 為 `finalized`／`cancelled`（未超過 7 天）仍可成功留言；⑦ 活動連結已失效（超過 7 天）→ 410 `LINK_EXPIRED`；⑧ 活動不存在 → 404 `EVENT_NOT_FOUND`；⑨ 同一暱稱可連續留言兩次，皆成功（驗證不要求活動內唯一）；⑩ 用 `unittest.mock.patch` 讓 `generate_short_id` 前一次回傳已存在的 id、第二次回傳新 id → 仍 201 成功建立（驗證碰撞重試路徑）
  - `GET`：⑪ 活動有 3 則留言（刻意用不同的建立順序／`created_at`）→ 200，回應陣列依 `created_at` 由舊到新排序；⑫ 活動無留言 → 200，空陣列；⑬ 活動不存在 → 404 `EVENT_NOT_FOUND`
  確認這組測試現在是 FAIL（model/view 都還不存在）— (auto) `pytest` 顯示這幾條 FAIL
- [x] 1.2 [GREEN] 實作：`apps/events/serializers.py` 新增 `CommentCreateSerializer`（`nickname`/`message`，`validate_nickname` 內 trim）、`CommentSerializer`（輸出用，`id`/`nickname`/`message`/`createdAt`）；`apps/events/views.py` 新增 `CommentListCreateView`（`permission_classes = [AllowAny]`、`authentication_classes = []`；`get()`/`post()` 皆先 `_get_event_or_404(id)` 再 `_display_status_or_410(event)`——刻意不呼叫 `_check_participation_preconditions`，見 design.md D5；`post()` 的 `create()` 比照 `ParticipantResponseCreateSerializer.create()`/`EventCreateSerializer.create()` 的碰撞重試迴圈寫法，但不需要「先查暱稱是否已存在」分支，`IntegrityError` 一律視為 id 碰撞直接重試，見 design.md D7；`get()` 回傳 `event.comments.order_by("created_at")` 序列化結果）；`apps/events/urls.py` 新增 `<shortid:id>/comments/` 路由；`config/exceptions.py` 的 `FIELD_CODE_OVERRIDES` 補上 `("message", "required")`/`("message", "blank")`: `"MESSAGE_REQUIRED"`、`("message", "max_length")`: `"MESSAGE_TOO_LONG"`（`nickname` 沿用既有的 `NICKNAME_REQUIRED` 對照，欄位名相同不需重複定義）。讓 1.1 全部轉綠為止，不多加東西 — (auto) `pytest apps/events/tests/test_views.py` 該檔全綠

> **Seam 1 的 `/code-review` 補充**：跑過一次自審，發現兩個非阻斷性小問題並修正：
> `GET` 端點的 410 分支缺測試（補上 `test_comment_list_link_expired_returns_410`）、
> `get()` 手動 `order_by("created_at")` 跟 `Comment.Meta.ordering` 重複（簡化為
> `event.comments.all()`），見 design.md code-review 補充。

## 2. Seam: `DELETE /api/events/{id}/comments/{commentId}`（對應 spec 需求：主揪刪除活動留言，commit 後追加，design.md D9）

- [x] 2.1 [RED] `Comment` model 新增 `deleted_at`（nullable `DateTimeField`）。跑
  `makemigrations` 產生新的獨立 migration（單純 `AddField`，不像
  `add-participant-responses` D15 那次被 Django 限制強迫 squash，見
  design.md D9/Migration Plan）。`CommentListCreateView.get()` 改為排除已刪除
  的留言（`deleted_at__isnull=True`）。補測試：
  - `DELETE`：① 主揪本人刪除存在且未刪除的留言 → 204，空 body，該留言之後不再出現在 `GET` 列表；② 已登入但非擁有者刪除 → 403 `FORBIDDEN`，留言不受影響；②之二 未登入刪除 → 401，留言不受影響（跟 `PATCH /api/events/{id}` 既有的擁有者權限測試一樣分成兩條，401/403 不可混在同一個 Scenario）；③ 刪除不存在的留言 id → 404 `COMMENT_NOT_FOUND`；④ 對已刪除過的留言再次刪除 → 404 `COMMENT_NOT_FOUND`；⑤ 用另一場活動的 id 搭配這場活動的留言 id → 404 `COMMENT_NOT_FOUND`
  - `GET` 補充：⑥ 活動有 2 則留言、其中 1 則已刪除 → 列表只回未刪除的那 1 則
  確認這組測試現在是 FAIL — (auto) `pytest` 顯示這幾條 FAIL
- [x] 2.2 [GREEN] 實作：`apps/events/views.py` 新增 `CommentDetailView`（比照 `EventDetailView.patch()` 的擁有者權限檢查模式：`IsAuthenticated`，`request.user != event.owner` 拋 `PermissionDenied`；查無該留言、已刪除、或 `comment.event_id != event.id` 皆拋 `ApiError(code="COMMENT_NOT_FOUND", status_code=404)`；成功則寫入 `deleted_at = timezone.now()`，回傳 `Response(status=204)`）；`apps/events/urls.py` 新增 `<shortid:id>/comments/<shortid:commentId>/` 路由。讓 2.1 全部轉綠為止 — (auto) `pytest apps/events/tests/test_views.py` 該檔全綠

## 3. 收尾

- [x] 3.1 全套驗證：`pytest -q`、`ruff check .`、`manage.py check`、`makemigrations --check --dry-run` 皆乾淨；`openspec validate add-event-comments --strict` 通過 — (auto)
- [x] 3.2 `/code-review`：跑一次自審，處理發現的真實問題（若有）— (auto)。發現
  兩個真實問題並修正：① `CommentDetailView.delete()` 有 TOCTOU 併發漏洞
  （先 SELECT 確認存在再 `.save()`，兩個並發請求都能成功刪除同一則留言），
  改成 compare-and-swap（`filter(deleted_at__isnull=True).update(...)`，
  影響 row 數判斷輸贏），補上真實併發測試
  `test_comment_delete_concurrent_requests_only_one_succeeds`，RED→GREEN
  驗證過；② `proposal.md` Impact 誤寫成有修改 `config/exceptions.py` 新增
  `COMMENT_NOT_FOUND`（實際上該 code 是 view 直接手動 `raise ApiError`，
  不經過中央對照表），已訂正措辭。見 design.md D9 code-review 補充
