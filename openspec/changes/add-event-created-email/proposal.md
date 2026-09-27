## Why

活動建立成功後，目前只有 API 回應裡的 `shareUrl`——主揪若沒有立刻複製連結就關掉分頁，就永久遺失分享連結的救援管道（沒有登入外的入口能重新找回，「我揪的團」需要登入才看得到）。跟現有 `finalize`/`cancel`/`reopen` 三支已經在做的「動作完成後寄通知信」一致，補上「建立成功」這個動作對應的信，把分享連結直接寄到主揪 Google 帳號信箱。

## What Changes

- `EventCreateView.post()` 成功建立活動後，比照 `finalize`/`cancel`/`reopen` 既有的 `_schedule_notification`（`transaction.on_commit` + 失敗吞例外只記 log）機制，非同步排入新 task 寄送分享連結至 `event.host_email`
- 新增 `apps/notifications/tasks.py::send_event_created_email(event_id)`
- 失敗不影響本次 API 回應（沿用 `_schedule_notification` 既有保護機制，不需要新寫）

未涵蓋（明確排除於本次 scope）：寄給除主揪以外的任何人（活動剛建立，尚無參與者）；信件內容客製化/範本系統。

## Capabilities

### New Capabilities

（無 — 沿用既有 `events` capability，新增其下的 Requirement）

### Modified Capabilities

- `events`：新增一條 Requirement（建立活動成功後寄送分享連結通知信）

## Impact

- 修改 `apps/notifications/tasks.py`：新增 `send_event_created_email`
- 修改 `apps/events/views.py`：`EventCreateView.post()` 呼叫 `_schedule_notification`
- 不需要新 migration（沿用既有 `Event.host_email` 欄位）
