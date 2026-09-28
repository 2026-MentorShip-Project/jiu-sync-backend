## Why

活動顯示狀態以 UTC 日期判斷「聚會日期是否已過」,台灣時間每天 00:00–08:00(UTC 仍是前一天)會把昨天的聚會判為 `finalized_upcoming`。活動詳情/列表的 `displayStatus` 因此錯誤,依賴它的功能(例如 `feature/ai-pick` 的 AI 推薦 409 `EVENT_ALREADY_PAST`)也跟著放行,相關測試在該時段執行會失敗。

## What Changes

- 「聚會日期是否已過」改以台灣時間(`TIME_ZONE = Asia/Taipei`)的當天日期判斷
- 補上 00:00–08:00 時段與日界邊界的測試
- 呼叫端、API 形狀、錯誤代碼皆不變
- 測試中寫死的未來日期(例如 `2026-10-01`)改為相對今天的日期,避免 2026-10-02 起 CI 因日期過期而失敗(task 1.1 code-review 發現)

## Capabilities

### New Capabilities

(無)

### Modified Capabilities

- `events`:新增 requirement,明確規定顯示狀態判斷「聚會日期是否已過」以台灣時間的日期為準(`events` 尚未 archive 到 `openspec/specs/`,與 `add-event-lifecycle` 使用同一路徑)

## Impact

- `apps/events/lifecycle.py`:`compute_display_status` 的日期比較
- `apps/events/tests/test_lifecycle.py`:新增時區測試
- `apps/events/tests/test_views.py`:依今天判斷的寫死日期改為相對日期(新增共用 helper `apps/events/tests/helpers.py`);`apps/notifications/tests/test_tasks.py` 的日期經盤點與今天無關,不需修改
- 間接修正:活動詳情/列表 `displayStatus`、以其為前置檢查的 API(不需改動呼叫端)
