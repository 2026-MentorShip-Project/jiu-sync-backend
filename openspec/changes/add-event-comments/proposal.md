## Why

活動頁目前沒有任何留言功能——參與者只能投票（見 `add-participant-responses`），沒有管道能單純留一句話（不投票、不表態時段）給主揪跟其他人看。需要一支獨立於投票的留言板 API。

## What Changes

- 新增 `POST /api/events/{id}/comments`：任何人（含未登入）皆可留言，暱稱（必填，trim 後上限 40）＋內容（必填，上限 200）
- 新增 `GET /api/events/{id}/comments`：查詢該活動全部留言，依留言時間由舊到新排序，一次回傳全部（不分頁），排除已刪除的留言
- 新增 `DELETE /api/events/{id}/comments/{commentId}`：限已登入且為該活動擁有者（主揪）可刪除，軟刪除（保留紀錄，不做實體刪除），成功回 204（commit 後追加，design.md D9）
- 留言前提條件只檢查活動連結未失效（410 `LINK_EXPIRED`）——活動狀態（進行中／已定案／已取消）與投票截止時間皆不影響能否留言，這點刻意跟參與者投票三支端點的前提條件（連結＋狀態＋截止時間）不同

未涵蓋（明確排除於本次 scope）：留言編輯（仍不支援）；留言與 `ParticipantResponse` 的關聯（完全獨立，不需要先投票或核對身分）；列表分頁；防灌水／內容審核機制。

## Capabilities

### New Capabilities

（無 — 沿用既有 `events` capability，新增其下的 Requirement）

### Modified Capabilities

- `events`：新增三條 Requirement（新增活動留言、查詢活動留言列表、主揪刪除活動留言）

## Impact

- 新增 `apps/events/models.py`：`Comment`（FK `Event`，`related_name="comments"`，含 `deleted_at`），含 migration
- 新增 `apps/events/serializers.py`：`CommentCreateSerializer`、`CommentSerializer`
- 新增 `apps/events/views.py`：`CommentListCreateView`（`GET`/`POST` 共用同一路徑，比照 `EventListView`/`EventCreateView` 的既有掛載模式）、`CommentDetailView`（`DELETE`，比照 `EventDetailView.patch()` 的擁有者權限檢查模式）
- 修改 `apps/events/urls.py`：新增兩條路由
- 修改 `config/exceptions.py`：`FIELD_CODE_OVERRIDES` 補上 `message` 欄位的 code（`nickname` 沿用參與者投票既有的 `NICKNAME_REQUIRED` 對照，欄位名相同，不需重複定義）。`COMMENT_NOT_FOUND` 不在此表裡——那張表只對照 DRF 欄位驗證的自動錯誤（`required`/`max_length` 這類），`COMMENT_NOT_FOUND` 是 `CommentDetailView.delete()` 直接手動 `raise ApiError(code="COMMENT_NOT_FOUND", ...)`（比照 `EVENT_NOT_FOUND`/`ACCESS_TOKEN_INVALID` 等既有作法），不需要、也沒有中央註冊表（code-review 抓到本段原本誤寫成有改 `config/exceptions.py` 新增這個 code）
