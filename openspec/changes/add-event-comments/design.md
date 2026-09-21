## Context

活動頁目前只有主揪基本資料＋候選時段＋投票彙整（`add-participant-responses`），沒有任何留言板功能。三支參與者投票端點已建立「完全公開、不需登入、不採用任何身分驗證」的慣例（`AllowAny` + `authentication_classes = []`），留言板延續同一套身分模型——留言比投票更輕量，沒有理由要求更嚴格的身分驗證。

## Goals / Non-Goals

**Goals:**
- 任何人（含未登入）可對一筆存在、連結未失效的活動留言
- 任何人可查詢一筆活動的全部留言

**Non-Goals:**
- 留言編輯／刪除
- 留言與投票（`ParticipantResponse`）的關聯
- 列表分頁
- 防灌水／內容審核機制

## Decisions

### D1. 完全公開，不需登入／身分驗證

跟現有三支參與者投票端點（`ParticipantResponseCreateView`/`VerifyView`/`DetailView`）一致，`permission_classes = [AllowAny]`、`authentication_classes = []`。已與使用者確認（grill-me）。

### D2. `Comment` 只有 `event` 這一個 FK，完全獨立於 `ParticipantResponse`

不需要先投票、不需要核對身分即可留言，`Comment` 跟 `ParticipantResponse` 之間沒有任何關聯欄位。已與使用者確認（grill-me）——這是「獨立留言板」的核心定義，使用者原話：「獨立留言板，但也是依附在事件上」，故只掛 `event` 這一個 FK。

### D3. 不支援修改／刪除，本次只做 `POST`（新增）＋`GET`（查詢列表）

跟參與者投票目前也沒有刪除權限一致。已與使用者確認（grill-me）——若未來需要，屬於獨立的後續 change。

### D4. 不分頁，一次回傳全部；內容上限 200 字

已與使用者確認（grill-me）：現階段預期單一活動的留言量不會大到需要分頁（同一批參與小型聚會的人），故不先設計分頁機制。內容長度上限比照 `ParticipantResponse.comment`（`add-participant-responses` D12）同款 200 字，維持全站留言類欄位長度上限一致。

### D5. 前提條件只檢查「連結未失效」，不檢查活動狀態／投票截止時間

已與使用者確認（grill-me），使用者原話：「只要連結未失效，就算 status 改成 cancel 取消也可以留言。所以能不能留言只有一個規則，就是該連結是否失效」。跟參與者投票三支端點共用的 `_check_participation_preconditions`（連結未失效→活動狀態為進行中→未過投票截止時間，三層）明確不同——留言板只重用 `_display_status_or_410(event)` 這一層，不呼叫 `_check_participation_preconditions`。活動已取消／已定案後，大家可能還想留言討論細節，這是留言板跟投票在語意上的本質差異：投票有截止時間的概念，留言沒有。

### D6. 暱稱必填，上限 40，trim，不要求同一活動內唯一

已與使用者確認（grill-me）：長度上限比照 `ParticipantResponse.nickname`（`add-participant-responses` D5）同款 40 字、前後空白 trim。但**不**比照 `ParticipantResponse.nickname` 的「同一活動內須唯一」規則（`unique_together (event, nickname)`）——留言板允許同一個人（同一個暱稱）留多則留言，這是投票（一人一票、暱稱代表身分）跟留言（單純發言，可以留很多次）的本質差異。`Comment` model 不需要 `unique_together`。

### D7. `Comment.id` 採 8 碼 base62 短 id，同 `Event.id`/`ParticipantResponse.id` 產生器

延續專案既有慣例（`ids.generate_short_id`），維持風格一致、短、URL 友善、不洩漏遞增序號——即使本次沒有「憑 id 單獨查詢／刪除單筆留言」的用途，仍比照全站其他對外可見 id 的做法，為未來若要加編輯/刪除留言功能預留一致的 id 格式，不需要屆時再煩惱要不要換 id 型別。碰撞重試邏輯比照 `ParticipantResponse.id`（`add-participant-responses` D9）同款寫法，但不需要那支的「先查暱稱是否已存在」分支——`Comment` 沒有 `unique_together`，`IntegrityError` 只可能是 id 碰撞，直接重試即可，不需要區分成因。

### D8. 列表排序：依 `created_at` 遞增（舊到新）

留言板慣例由舊到新往下疊，符合一般留言板/討論串的閱讀順序。**這項不是 grill-me 確認過的項目**，屬於實作細節的合理預設值——若跟前端預期的排序方向不符，是低成本可調整的項目（單一 `.order_by()` 參數），不影響本次其餘設計決策。

> **code-review 補充（2026-09-21）**：兩個非阻斷性小問題，皆已修正——① `GET` 端點的 `_display_status_or_410` 410 分支原本只有 `POST` 那條測試涵蓋，補上 `test_comment_list_link_expired_returns_410`；② `CommentListCreateView.get()` 原本手動 `event.comments.order_by("created_at")`，重複了 `Comment.Meta.ordering`（D8）已經定義的預設排序，簡化為 `event.comments.all()`。

## Risks / Trade-offs

- **[風險] 無防灌水／rate limit 機制** → 跟手機末三碼暴力風險（`add-participant-responses` D1）同類的「本次刻意接受」風險，任何人都能無限次留言。緩解方向留給未來 change：IP／裝置層級 rate limit，或要求先核對身分。
- **[風險] 無內容審核，暱稱／內容皆為自由文字** → 跟現有 `ParticipantResponse.comment`（`add-participant-responses` D12）同等級的既有風險，非本次新增，不重複處理。

## Migration Plan

新增 migration：`Comment`（FK `Event`，`related_name="comments"`）。純新增資料表，不修改既有 `Event`/`Slot`/`ParticipantResponse` schema，回滾只需 reverse migration，不影響既有端點。
