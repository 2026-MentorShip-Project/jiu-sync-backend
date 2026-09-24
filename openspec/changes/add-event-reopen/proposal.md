## Why

`add-event-lifecycle` 明確把「重新開放投票」排除在外，留給獨立的後續 change。使用者確認需求：主揪已經定案後，發現需要重新開放投票（例如原訂時段有變、想再收集意見），需要一支端點把活動從 `finalized` 帶回 `active`，讓大家可以繼續／重新投票。

## What Changes

- 新增 `POST /api/events/{id}/reopen`：已登入且為活動擁有者，只能對狀態為 `finalized` 的活動執行。請求帶新的 `responseDeadline`（必填，須晚於現在——原本的截止時間多半已經過了，重新開放需要一個新的未來時間）
- 成功後：狀態轉回 `active`，清空 `final_slot`/`final_note`/`finalized_at`，`response_deadline` 更新為請求帶入的新值；**既有參與者投票資料完全不動**（不像 `cancel` 會軟刪除——`finalized` 狀態本來就不會有任何投票被軟刪除，這條路徑沒有這個問題）
- 成功後非同步寄送重新開放通知信給留 Email 的參與者與主揪本人

未涵蓋（明確排除於本次 scope）：從 `cancelled` 重新開放（使用者明確確認只針對「已定案後」的情境，`cancelled` 是終態，不在此次範圍）；重新開放時修改候選時段（`slots`，這超出「重新開放投票」本身，屬於另一個功能）。

## Capabilities

### New Capabilities

（無 — 沿用既有 `events` capability，新增其下的 Requirement）

### Modified Capabilities

- `events`：新增一條 Requirement（主揪重新開放投票）

## Impact

- 新增 `apps/events/serializers.py`：`EventReopenSerializer`（`responseDeadline`）
- 新增 `apps/events/views.py`：`EventReopenView`（比照 `EventFinalizeView`/`EventCancelView` 的既有模式：輕量 queryset 做擁有者/狀態檢查、CAS 狀態轉換、`_schedule_notification()` 排入通知信、重新查一次重量 queryset 做最終回應）
- 修改 `apps/events/urls.py`：新增一條路由
- 新增 `apps/notifications/tasks.py`：`send_event_reopened_email`
- 修改 `config/exceptions.py`：`FIELD_CODE_OVERRIDES` 補上 `responseDeadline` 對應（沿用既有 `RESPONSE_DEADLINE_REQUIRED`/手動驗證的 `DEADLINE_IN_PAST`，這兩個 code 已存在，不需新增）；新增 `EVENT_NOT_FINALIZED` 業務 code
