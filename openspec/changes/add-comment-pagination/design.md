## Context

`add-event-comments` 的 proposal.md 當初明確寫「未涵蓋：...列表分頁」，`Comment.Meta.ordering = ["created_at"]`（遞增，由舊到新）——這是目前 `GET /api/events/{id}/comments` 已經上線、被使用中的排序方向。

跟使用者 grill-me 確認：分頁後預設要看到「最新的留言」，不是延續現有「由舊到新」的直覺閱讀順序。這代表查詢/回應排序整個反過來（由新到舊），不是單純在既有排序上加一個 `cursor` 參數——是會改變既有行為的 breaking change。

## Decisions

### D1. 回應改成 `{comments, nextCursor}`，不額外加 `hasMore`

`nextCursor` 為 `null` 就代表沒有更多，不需要額外的布林欄位。跟已經上線的 `GET /api/events/{id}/poll` 同一種「輕量物件」回應風格一致，前端已經熟悉「判斷某欄位是否為 `null`」而非另外一個布林值的模式。

### D2. Cursor 格式：`{created_at 的 ISO 字串}_{id}`，view 端負責組/解析

不用 base64 包一層——cursor 不是敏感資訊、也不是權限憑證（跟 `ParticipantResponseAccessToken` 那種需要不可預測性的 token 不同），純粹是查詢起點，base64 只會增加除錯難度（開發者/前端無法直接讀出 cursor 代表哪個時間點），不加分。用底線分隔是因為 `created_at.isoformat()` 本身不含底線字元，可以安全 `rsplit("_", 1)` 還原成兩個部分。

### D3. 為什麼用 cursor（keyset pagination）不用 offset/limit

留言板是多人即時互動情境（同一活動可能同時有好幾個參與者在留言）。Offset 分頁在並發新增時有經典問題：使用者往回翻頁（offset 遞增）的過程中，如果有新留言插入在「更新」的那一端，會讓原本第二頁該出現的某則留言被往後推一位、要嘛在第一頁被重複看到、要嘛在翻頁時被跳過。

Cursor（沿用上一頁最後一則的 `(created_at, id)` 當下一頁查詢起點：`created_at < cursor_created_at OR (created_at = cursor_created_at AND id < cursor_id)`）不受這個問題影響——不管中間插入多少新留言，下一頁固定接續在「使用者已經看過的最後一則」之後（依時間更舊的方向）查詢，新插入的留言只會出現在「更新」的那一端（第一頁），不會干擾已經在翻的分頁序列。

複合條件（`created_at` 為主、`id` 為次要排序鍵）是為了處理理論上兩則留言 `created_at` 完全相同（同毫秒／微秒建立）的邊界情況，避免排序不穩定。

### D4. 固定每頁 10 則，不開放 `limit` 查詢參數

跟使用者確認的「每次十筆」是固定值，不是預設值——不開放前端指定，避免惡意呼叫端傳超大 `limit` 造成一次查詢過重（跟這支端點刻意設計成公開、免登入、任何人可查有關，沒有身分可以事後追責，事前限制更重要）。

### D5. Breaking change 因應：不做新舊格式相容層

前端目前把 `GET` 回應直接當陣列用。留言板本身還在開發階段，還沒有正式使用者依賴既有純陣列格式，不需要為了相容性同時支援兩種回應形狀（例如用 query 參數切換），直接切換到新格式、前端同步更新即可。

## Risks / Trade-offs

- **[風險] `cursor` 被竄改成不存在或格式不合法的值** → 查詢應該優雅退化，不能 500。設計：`cursor` 解析失敗（格式不對、`created_at` 不是合法日期）時，視同沒有帶 `cursor`，直接回傳最新一頁（不拋錯）；`cursor` 格式合法但指向的時間點單純查無更舊資料時，正常回傳空陣列 + `nextCursor: null`。兩種情況都要在 tasks.md 涵蓋測試。
- **[風險] 前端沒有同步更新，繼續把回應當陣列用** → 這是本次 scope 外、需要跨團隊協調的風險，proposal.md 的 Impact 已經明確標註「前端需同步更新」。

## Migration Plan

不需要新 migration——沿用既有 `Comment.created_at`/`id` 欄位，兩者原本就有資料庫索引（`id` 是 primary key；`created_at` 透過 `Meta.ordering` 已隱含排序需求，若後續效能量測發現需要顯式索引，屬於獨立的效能優化 change，非本次 scope）。
