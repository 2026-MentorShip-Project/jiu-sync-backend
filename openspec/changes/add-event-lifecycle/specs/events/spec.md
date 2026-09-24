## ADDED Requirements

### Requirement: 主揪定案活動

系統 SHALL 只允許已登入且為該活動擁有者（主揪）的使用者，將一筆狀態為進行中（`active`）的活動定案。未登入時系統 SHALL 拒絕請求，回傳 401；已登入但非該活動擁有者時系統 SHALL 拒絕請求，回傳 403，錯誤代碼為 `FORBIDDEN`。

定案請求 SHALL 包含最終時段識別碼（必填，須為該活動已存在的候選時段之一）與備註（選填，長度上限 200）。最終時段識別碼不屬於該活動時，系統 SHALL 拒絕請求，回傳代碼 `SLOT_NOT_FOUND`。

活動狀態已經是 `finalized` 時，系統 SHALL 拒絕請求，回傳 409，錯誤代碼為 `EVENT_ALREADY_FINALIZED`；活動狀態已經是 `cancelled` 時，系統 SHALL 拒絕請求，回傳 409，錯誤代碼為 `EVENT_ALREADY_CANCELLED`。

定案成功後，系統 SHALL 將活動狀態轉為 `finalized`，並非同步寄送定案通知信給該活動所有留有 Email 的參與者，以及主揪本人（若主揪填寫了 Email）。回應 SHALL 為該活動當下的完整資料（同查詢單一活動完整資料）。

#### Scenario: 主揪成功定案活動

- **WHEN** 活動擁有者本人對一筆狀態為進行中的活動，送出屬於該活動候選時段之一的最終時段識別碼
- **THEN** 系統將活動狀態轉為 `finalized`，回應為該活動當下的完整資料，且非同步寄送定案通知信給留有 Email 的參與者與主揪本人

#### Scenario: 已登入但非擁有者定案遭拒絕

- **WHEN** 已登入但非該活動擁有者的使用者對一筆活動送出定案請求
- **THEN** 系統拒絕請求，回傳 403，錯誤代碼為 `FORBIDDEN`，活動狀態不受影響

#### Scenario: 未登入定案遭拒絕

- **WHEN** 未登入的請求者對一筆活動送出定案請求
- **THEN** 系統拒絕請求，回傳 401，活動狀態不受影響

#### Scenario: 最終時段識別碼不屬於該活動

- **WHEN** 主揪送出的最終時段識別碼不是該活動已存在的候選時段
- **THEN** 系統拒絕請求，回傳代碼 `SLOT_NOT_FOUND`，活動狀態不受影響

#### Scenario: 定案已經定案過的活動

- **WHEN** 主揪對一筆狀態已經是 `finalized` 的活動再次送出定案請求
- **THEN** 系統拒絕請求，回傳 409，錯誤代碼為 `EVENT_ALREADY_FINALIZED`

#### Scenario: 定案已經取消的活動

- **WHEN** 主揪對一筆狀態已經是 `cancelled` 的活動送出定案請求
- **THEN** 系統拒絕請求，回傳 409，錯誤代碼為 `EVENT_ALREADY_CANCELLED`

### Requirement: 主揪取消活動

系統 SHALL 只允許已登入且為該活動擁有者（主揪）的使用者，取消一筆狀態為進行中（`active`）或已定案（`finalized`）的活動。未登入時系統 SHALL 拒絕請求，回傳 401；已登入但非該活動擁有者時系統 SHALL 拒絕請求，回傳 403，錯誤代碼為 `FORBIDDEN`。

活動狀態已經是 `cancelled` 時，系統 SHALL 拒絕請求，回傳 409，錯誤代碼為 `EVENT_ALREADY_CANCELLED`。

取消成功後，系統 SHALL 將活動狀態轉為 `cancelled`；該活動既有的參與者投票紀錄 SHALL 全數軟刪除（保留紀錄，不做實體刪除），之後 SHALL NOT 出現在查詢單一活動完整資料的 `responses`/`slotSummary` 結果中。取消 SHALL NOT 影響該活動既有的留言（見「新增活動留言」/「查詢活動留言列表」Requirement，留言不受活動狀態影響）。系統 SHALL 非同步寄送取消通知信給該活動所有留有 Email 的參與者（含已因本次取消而被軟刪除的投票紀錄），以及主揪本人（若主揪填寫了 Email）。回應 SHALL 為該活動當下的完整資料。

#### Scenario: 主揪成功取消進行中的活動

- **WHEN** 活動擁有者本人對一筆狀態為進行中、且已有參與者投票的活動送出取消請求
- **THEN** 系統將活動狀態轉為 `cancelled`，該活動既有的投票紀錄全數軟刪除，回應中的 `responses`/`slotSummary` 反映為空，且非同步寄送取消通知信給原本留有 Email 的參與者與主揪本人

#### Scenario: 主揪成功取消已定案的活動

- **WHEN** 活動擁有者本人對一筆狀態為已定案的活動送出取消請求
- **THEN** 系統將活動狀態轉為 `cancelled`

#### Scenario: 已登入但非擁有者取消遭拒絕

- **WHEN** 已登入但非該活動擁有者的使用者對一筆活動送出取消請求
- **THEN** 系統拒絕請求，回傳 403，錯誤代碼為 `FORBIDDEN`，活動狀態不受影響

#### Scenario: 未登入取消遭拒絕

- **WHEN** 未登入的請求者對一筆活動送出取消請求
- **THEN** 系統拒絕請求，回傳 401，活動狀態不受影響

#### Scenario: 取消已經取消過的活動

- **WHEN** 主揪對一筆狀態已經是 `cancelled` 的活動再次送出取消請求
- **THEN** 系統拒絕請求，回傳 409，錯誤代碼為 `EVENT_ALREADY_CANCELLED`

#### Scenario: 取消活動不影響既有留言

- **WHEN** 主揪取消一筆已有留言的活動
- **THEN** 該活動既有的留言 SHALL NOT 被刪除或隱藏，查詢活動留言列表仍可看到

## MODIFIED Requirements

### Requirement: 查詢單一活動完整資料

系統 SHALL 允許任何人（不論是否登入）透過活動識別碼查詢該活動的完整資料，前提是該活動存在。查無對應活動時系統 SHALL 回傳查無此活動的錯誤，錯誤代碼 SHALL 為活動專屬的 `EVENT_NOT_FOUND`，不是泛用代碼。

回應內容 SHALL 包含依目前使用者計算出的擁有者旗標：僅當請求者為已登入使用者且為該活動擁有者時為真，其餘情況（含未登入）一律為假。

回應內容中的主揪 Email SHALL 僅在擁有者旗標為真時回傳實際值，其餘情況一律回傳空值，不得外洩。

> **修訂記錄（2026-09-23）**：`responses`/`slotSummary` SHALL 排除因活動被取消而軟刪除的投票紀錄——已軟刪除的投票紀錄對查詢者而言視為不存在，跟「主揪取消活動」Requirement 的行為一致，見 design.md D6。

#### Scenario: 已取消活動查詢時投票資料已排除

- **WHEN** 任何請求者查詢一筆已被主揪取消、且取消前已有參與者投票的活動
- **THEN** 回應中的 `responses` 為空陣列，`slotSummary` 每個候選時段的三態票數皆為 0
