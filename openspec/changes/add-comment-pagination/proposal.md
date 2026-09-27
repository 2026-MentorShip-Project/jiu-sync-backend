## Why

`add-event-comments` 當初明確排除「列表分頁」，`GET /api/events/{id}/comments` 一次回傳活動全部留言。留言數量隨活動熱度成長後，這支查詢跟回應 payload 會線性變重，且前端多數情況下不需要一次拿到全部內容——改成前端主動往回翻頁才載入更多。

## What Changes

- `GET /api/events/{id}/comments` 回應格式改變（**breaking change**）：從純陣列改成 `{comments: [...], nextCursor: string|null}`
- 預設（不帶 `cursor` 查詢參數）回傳**最新 10 則**（依留言時間由新到舊排序）
- 帶 `cursor` 查詢參數時，回傳該 cursor 之前（更舊）的下 10 則，一樣由新到舊排序
- `cursor` 是 opaque string，由上一頁最後一則的 `(created_at, id)` 組成，不是頁碼——同時有人在留言（並發新增）時不會造成翻頁時跳過或重複看到某則留言
- 固定每頁 10 則，不開放前端指定 limit

未涵蓋：留言在資料庫裡的實際寫入/排序（`Comment.Meta.ordering` 仍是 `created_at` 遞增，只有這支查詢端點的回應順序改變）；`cursor` 竄改的權限保護（不需要，`cursor` 只影響查詢起點，不是權限判斷）。

## Capabilities

### New Capabilities

（無 — 沿用既有 `events` capability）

### Modified Capabilities

- `events`：MODIFIED Requirement「查詢活動留言列表」——從「一次回傳全部、由舊到新」改為「cursor 分頁、每頁 10 則、由新到舊」

## Impact

- 修改 `apps/events/views.py`：`CommentListCreateView.get()`
- 新增 cursor 編解碼 helper（`apps/events/views.py` 或獨立模組）
- 不需要新 migration（沿用既有 `Comment.created_at`/`id`）
- **前端需同步更新**：呼叫方目前把 `GET` 回應直接當陣列用，這次上線後需要改成讀取 `.comments`，並實作「載入更多」時帶入 `nextCursor` 當 `cursor` 查詢參數
