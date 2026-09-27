## MODIFIED Requirements

### Requirement: 查詢活動留言列表

系統 SHALL 允許任何人（不論是否登入）分頁查詢一筆活動的留言，依留言時間由新到舊排序，每頁固定 10 則。系統 SHALL 支援可選的 `cursor` 查詢參數：不帶 `cursor` 時回傳最新 10 則；帶合法 `cursor` 時回傳該 cursor 所代表位置之前（更舊）的下 10 則。`cursor` SHALL 由查詢結果最後一則留言的建立時間與識別碼組成，不是單純的頁碼，確保並發新增留言時不會造成翻頁時跳過或重複出現同一則留言。

回應 SHALL 為 `{comments: [...], nextCursor: string | null}`——`comments` 每筆包含識別碼、暱稱、內容、留言時間；`nextCursor` 為 `null` 代表沒有更多留言可載入，非 `null` 時可作為下一次查詢的 `cursor` 參數。`cursor` 參數格式不合法時系統 SHALL 視同未帶此參數，回傳最新一頁，SHALL NOT 回傳伺服器錯誤。已被主揪刪除的留言 SHALL NOT 出現在任何一頁的結果中，也 SHALL NOT 計入每頁 10 則的筆數。

#### Scenario: 查詢留言數超過一頁的活動（不帶 cursor）

- **WHEN** 任何請求者查詢一筆有 15 則留言的活動，不帶 `cursor` 查詢參數
- **THEN** 系統回傳最新 10 則（依留言時間由新到舊排序）、`nextCursor` 為非 `null` 的值

#### Scenario: 帶上一頁的 nextCursor 查詢下一頁

- **WHEN** 請求者帶著上一次查詢回應的 `nextCursor` 再次查詢同一活動
- **THEN** 系統回傳更舊的下 10 則留言，且 SHALL NOT 與上一頁重複或跳過任何一則

#### Scenario: 留言數不超過一頁

- **WHEN** 任何請求者查詢一筆留言數 ≤10 則的活動，不帶 `cursor` 查詢參數
- **THEN** 系統回傳全部留言、`nextCursor` 為 `null`

#### Scenario: 查詢無留言的活動

- **WHEN** 任何請求者查詢一筆存在但尚無任何留言的活動
- **THEN** 系統回傳 `comments` 為空陣列、`nextCursor` 為 `null`

#### Scenario: cursor 參數格式不合法

- **WHEN** 請求者帶一個格式不合法或無法解析的 `cursor` 查詢參數
- **THEN** 系統視同未帶此參數，回傳最新一頁，SHALL NOT 回傳 500 或其他伺服器錯誤

#### Scenario: 並發新增留言不影響已在進行的分頁

- **WHEN** 請求者查詢第一頁之後、尚未查詢下一頁之前，該活動有新留言被建立
- **THEN** 請求者接著帶第一頁的 `nextCursor` 查詢下一頁時，結果 SHALL NOT 包含這則新留言，也 SHALL NOT 因此跳過原本該出現在下一頁的留言

#### Scenario: 已刪除的留言不出現在任何一頁

- **WHEN** 任何請求者查詢一筆活動，其中一則留言已被主揪刪除
- **THEN** 該則已刪除的留言 SHALL NOT 出現在任何一頁的 `comments` 中，也 SHALL NOT 計入分頁筆數

#### Scenario: 查詢不存在的活動

- **WHEN** 任何請求者以不存在的活動識別碼查詢留言列表
- **THEN** 系統回傳查無此活動的錯誤，錯誤代碼為 `EVENT_NOT_FOUND`
