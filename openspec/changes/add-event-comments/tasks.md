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

## 2. 收尾

- [x] 2.1 全套驗證：`pytest -q`、`ruff check .`、`manage.py check`、`makemigrations --check --dry-run` 皆乾淨；`openspec validate add-event-comments --strict` 通過 — (auto)
- [x] 2.2 `/code-review`：跑一次自審，處理發現的真實問題（若有）— (auto)。發現
  兩個非阻斷性小問題並修正：`GET` 端點的 410 分支缺測試（補上）、`get()`
  手動 `order_by` 跟 `Comment.Meta.ordering` 重複（簡化），見 design.md
  code-review 補充
