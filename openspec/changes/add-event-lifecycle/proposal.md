## Why

活動目前只有「進行中（active）」一種可操作狀態，`Event.status` 雖然早就定義了 `finalized`/`cancelled`（`compute_display_status()` 也已經能算出對應的六種 `displayStatus`），但沒有任何 API 能真的把活動轉進這兩個狀態。主揪需要能拍板定案（選定最終時段）、以及取消整場活動，並讓參與者知道結果。

本輪只做「定案」與「取消」；「重新開放投票」明確排除（見下方未涵蓋），留給獨立的後續 change。

## What Changes

- 新增 `POST /api/events/{id}/finalize`：已登入且為活動擁有者，從 `active` 狀態選定一個候選時段定案（`finalSlotId` 必填，須為該活動已存在的候選時段；`finalNote` 選填，上限 200 字）。成功後非同步寄送定案通知信給所有留過 Email 的參與者與主揪本人
- 新增 `POST /api/events/{id}/cancel`：已登入且為活動擁有者，從 `active` 或 `finalized` 狀態取消整場活動。成功後既有的參與者投票資料（`ParticipantResponse`）改為軟刪除（新增 `deleted_at`），並非同步寄送取消通知信給所有留過 Email 的參與者與主揪本人
- 首次真正串接既有的 Celery＋Mailers 設定（`config/settings/base.py` 早就預留，但目前沒有任何一個地方真的呼叫過）——新增 `apps/notifications/tasks.py` 放這兩支通知信的 Celery task
- `EventDetailSerializer` 的 `responses`/`slotSummary` 同步排除已軟刪除的投票資料

未涵蓋（明確排除於本次 scope）：`POST /api/events/{id}/reopen`（重新開放投票）——本次不做，留給獨立的後續 change；留言（`Comment`）不受活動狀態影響，取消活動不會連動刪除或隱藏留言（`add-event-comments` D5 既有行為維持不變）；Email 內容的樣式/多語系（本次純文字、單一中文文案）；寄送失敗的重試/死信佇列策略。

## Capabilities

### New Capabilities

（無 — 沿用既有 `events` capability，新增其下的 Requirement）

### Modified Capabilities

- `events`：新增兩條 Requirement（主揪定案活動、主揪取消活動），並修改「查詢單一活動完整資料」使 `responses`/`slotSummary` 排除已因活動取消而軟刪除的投票資料

## Impact

- 修改 `apps/events/models.py`：`ParticipantResponse` 新增 `deleted_at`（nullable `DateTimeField`，軟刪除標記），含 migration
- 新增 `apps/events/serializers.py`：`EventFinalizeSerializer`（`finalSlotId`/`finalNote`）
- 新增 `apps/events/views.py`：`EventFinalizeView`、`EventCancelView`（皆比照 `EventDetailView.patch()` 的擁有者權限檢查模式；狀態轉換用 compare-and-swap 避免併發雙重提交）
- 修改 `apps/events/urls.py`：新增兩條路由
- 修改 `apps/events/views.py` 的 `_event_with_responses_queryset()`：`responses` 的 prefetch 改用 `Prefetch` 排除已軟刪除的投票資料
- 新增 `apps/notifications/tasks.py`：`send_event_finalized_email`/`send_event_cancelled_email` 兩個 Celery task（首次真正使用這個既有但空的 app）
- 修改 `config/settings/base.py`/`dev.py`：新增 `CELERY_TASK_ALWAYS_EAGER`（`dev.py` 預設 `True`，本機/測試不需要真的跑 Celery worker 也能執行；`prod.py` 沿用 `base.py` 的 `False`，走真正的非同步佇列）
- 修改 `config/exceptions.py`：`FIELD_CODE_OVERRIDES` 補上 `finalSlotId`/`finalNote` 的 code
