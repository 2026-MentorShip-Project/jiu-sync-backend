> TDD 排法：RED（先寫會失敗的測試）→ GREEN（只寫剛好讓它通過的最小實作）。Seam 走 HTTP 層測（DRF test client）。
>
> 這個 change 不引入新工具鏈/新依賴，不需要獨立的環境健檢 task。

## 1. Seam: cursor 編解碼 helper（對應 spec 需求：查詢活動留言列表——分頁）

- [x] 1.1 [RED] 補單元測試（純函式，不經 HTTP）：① 一組 `(created_at, id)` 編碼後可以解碼還原成同樣的值；② 解碼一個格式不合法的字串（例如缺底線、`created_at` 不是合法 ISO 格式）回傳 `None`（不拋例外）。確認這組測試現在 FAIL（函式不存在）— (auto) `pytest` 顯示 FAIL
- [x] 1.2 [GREEN] 實作：`apps/events/views.py`（或抽獨立模組）新增 `_encode_comment_cursor(comment)` / `_decode_comment_cursor(cursor)`，格式為 `{created_at.isoformat()}_{id}`，解碼失敗回傳 `None` 不拋例外。讓 1.1 轉綠 — (auto) `pytest` 該函式測試全綠

## 2. Seam: `GET /api/events/{id}/comments` 分頁（對應同一條 spec 需求）

- [x] 2.1 [RED] 在 `apps/events/tests/test_views.py` 補測試，涵蓋：① 活動有 15 則留言、不帶 `cursor` 查詢 → 回傳最新 10 則（依 `created_at` 由新到舊）、`nextCursor` 非 `null`；② 帶上一頁回傳的 `nextCursor` 再查一次 → 回傳剩下 5 則（更舊的那批）、`nextCursor` 為 `null`；③ 活動留言數 ≤10、不帶 `cursor` → 回傳全部、`nextCursor` 為 `null`；④ 活動完全沒有留言 → 回傳空陣列、`nextCursor` 為 `null`；⑤ 帶一個格式不合法的 `cursor`（例如亂數字串）→ 視同沒帶 `cursor`，回傳最新一頁，不 500；⑥ 已軟刪除的留言不計入任何一頁、不影響分頁筆數；⑦ 併發情境：模擬「查第一頁之後、翻第二頁之前，資料庫插入一則新留言」→ 第二頁結果不受這則新插入留言影響（不重複也不跳過既有留言）；⑧ 活動不存在 → 404 `EVENT_NOT_FOUND`（既有行為，確認分頁改動沒有連帶破壞）。確認這組測試現在 FAIL（回應還是舊格式的純陣列、由舊到新）— (auto) `pytest` 顯示 FAIL
- [x] 2.2 [GREEN] 實作：`CommentListCreateView.get()` 改為：讀取 `cursor` query 參數 → 解碼成功則加上 `created_at < X OR (created_at = X AND id < Y)` 過濾條件 → `order_by("-created_at", "-id")[:10]`（固定 10 筆，見 design.md D4）→ 組出 `nextCursor`（取到滿 10 筆時，用第 10 筆算 cursor；不滿 10 筆代表沒有更多，`nextCursor` 為 `None`）→ 回傳 `{"comments": [...], "nextCursor": ...}`。讓 2.1 全部轉綠 — (auto) `pytest apps/events/tests/test_views.py` 該檔全綠

## 3. 收尾

- [x] 3.1 全套驗證：`pytest -q`、`ruff check .`、`manage.py check`、`makemigrations --check --dry-run`（預期無變化）皆乾淨；`spectra validate add-comment-pagination --strict` 通過 — (auto)
- [x] 3.2 `/code-review`：跑一次自審，處理發現的真實問題（若有）— (auto)
- [ ] 3.3 更新 Swagger 文件：`GET /api/events/{id}/comments` 回應 schema 改成 `{comments, nextCursor}`，補上 `cursor` query 參數說明 — (manual，Swagger 是獨立 artifact 文件，非 repo 內檔案)
