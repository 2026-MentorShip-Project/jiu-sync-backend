## ADDED Requirements

### Requirement: 輕量輪詢端點

系統 SHALL 提供一個公開（不需登入）的輕量輪詢端點，讓前端能以固定頻率（例如每 10 秒）查詢一筆活動「有沒有變化」，不需要每次都取得完整投票/留言明細。回應 SHALL 包含：活動狀態 `status`、顯示狀態 `displayStatus`、活動本身最後異動時間 `eventUpdatedAt`（活動欄位被修改、或經歷 finalize/cancel/reopen 任一狀態轉換時更新）、未軟刪除的投票筆數 `responseCount`、最後一次投票異動時間 `latestResponseAt`（含新投票與修改既有投票，無投票時為 `null`）、未軟刪除的留言筆數 `commentCount`、最新一則留言時間 `latestCommentAt`（無留言時為 `null`）。回應 SHALL NOT 包含個別參與者或留言的明細內容（暱稱、留言文字、候選時段選擇等）。

活動連結已失效（顯示狀態計算為 `link_expired`）時系統 SHALL 拒絕請求，回傳 410，錯誤代碼為 `LINK_EXPIRED`，跟 `GET /api/events/{id}` 的既有連結失效規則一致。查無對應活動時系統 SHALL 回傳 404，錯誤代碼為 `EVENT_NOT_FOUND`。

#### Scenario: 輪詢一筆有投票與留言的活動

- **WHEN** 任何人（含未登入）查詢一筆有 2 筆未軟刪除投票、3 則未軟刪除留言、連結未失效的活動
- **THEN** 系統回傳 200，`responseCount` 為 2、`commentCount` 為 3、`latestResponseAt`/`latestCommentAt` 分別是最後一次投票異動與最新留言的時間

#### Scenario: 沒有任何投票或留言的活動

- **WHEN** 查詢一筆完全沒有投票、也沒有留言的活動
- **THEN** 系統回傳 200，`responseCount`/`commentCount` 皆為 0，`latestResponseAt`/`latestCommentAt` 皆為 `null`

#### Scenario: 修改既有投票會反映在最後異動時間

- **WHEN** 一位參與者透過 `PATCH /api/events/{id}/responses/{responseId}` 修改了既有投票的候選時段選擇
- **THEN** 之後查詢輪詢端點，`latestResponseAt` 反映這次修改的時間，`responseCount` 不因此改變（不是新增一筆，是既有那筆被異動）

#### Scenario: 軟刪除的投票與留言不計入

- **WHEN** 一筆活動被取消（既有投票遭軟刪除）、或某則留言被擁有者軟刪除
- **THEN** 這些被軟刪除的紀錄不計入 `responseCount`/`commentCount`，也不影響 `latestResponseAt`/`latestCommentAt`

#### Scenario: 活動狀態變化反映在 status/displayStatus

- **WHEN** 活動被定案、取消或重新開放投票
- **THEN** 之後查詢輪詢端點，`status`/`displayStatus` 反映最新狀態

#### Scenario: 定案後重新開放、又再次定案，即使投票留言數不變也能偵測到活動變化

- **WHEN** 一筆活動被定案（`finalSlotId` 為 A），之後被重新開放投票、再次定案為另一個候選時段（`finalSlotId` 為 B），期間沒有新的投票或留言
- **THEN** 兩次定案後查詢輪詢端點，即使 `responseCount`/`commentCount`/`status`/`displayStatus` 前後相同（皆為 `finalized`），`eventUpdatedAt` 仍不同，讓前端能判斷出活動本身已經變化、需要重新取得完整內容

#### Scenario: 活動連結已失效時拒絕輪詢

- **WHEN** 查詢一筆連結已失效的活動
- **THEN** 系統回傳 410，錯誤代碼為 `LINK_EXPIRED`

#### Scenario: 活動不存在

- **WHEN** 查詢一筆不存在的活動 id
- **THEN** 系統回傳 404，錯誤代碼為 `EVENT_NOT_FOUND`

### Requirement: 改票會更新最後異動時間

系統 SHALL 讓參與者透過 `PATCH /api/events/{id}/responses/{responseId}` 成功修改既有投票的候選時段選擇時，更新該筆投票紀錄的最後異動時間，供輪詢端點的 `latestResponseAt` 正確反映「改票」這個變化（不只是「新投票」）。

#### Scenario: 改票後最後異動時間變新

- **WHEN** 一位參與者成功修改了既有投票的候選時段選擇
- **THEN** 該筆投票紀錄的最後異動時間比修改前新
