## Context

現有 `_schedule_notification`（`apps/events/views.py`）是 `finalize`/`cancel`/`reopen` 共用的非同步排程 helper：`transaction.on_commit()` 觸發、task 排程/執行當下的例外一律吞掉只記 log，不讓通知信失敗影響已經成功的主動作。`apps/notifications/tasks.py` 已有 `_event_share_url(event)`/`_send_to_each_recipient(subject, body, recipients)` 兩個共用函式。本次直接複用，不另外設計新機制。

## Decisions

### D1. 只寄給主揪本人，不重用 `_notification_recipients()`

`_notification_recipients()`（現有函式）回傳「留過 Email 的參與者 + 主揪本人」——但活動剛建立的當下還沒有任何 `ParticipantResponse`，語意上這裡只需要「主揪本人」，直接判斷 `event.host_email` 是否存在即可，不重用那支語意上是給「活動已有參與者互動之後」的通知信設計的函式。`host_email` 來自 `EventCreateView.post()` 寫入時的 `request.user.email`（Google 帳號 email，恆存在），但仍保留存在性檢查，防禦性地跟其他三支通知信的寫法一致。

### D2. 不需要「過期 task」判斷

`finalize`/`cancel`/`reopen` 三支通知信 task 都要核對「這次 transition 有沒有被後續動作蓋過」（見 `add-event-lifecycle` design.md D10）——因為同一個 `Event.id` 可能被 `reopen`→`finalize` 反覆好幾輪，舊 task 執行時看到的狀態可能剛好又符合、造成用舊資料寄出跟現況不符的信。建立活動這個動作對同一個 `event.id` 只會發生一次（沒有「重新建立」這種 transition），不存在「被蓋過」的疑慮。Task 內只需要核對 `event` 是否存在（純防禦性，理論上不會發生，因為系統沒有刪除活動的功能，即使 `cancel`/軟刪除留言/投票也都不刪除 `Event` 本身）。

### D3. 失敗不影響 API 回應

沿用 `_schedule_notification` 既有機制（`transaction.on_commit` 內部 try/except 吞例外只記 log），不需要為這支新 task 額外處理。

## Risks / Trade-offs

- **[風險] 主揪信箱是垃圾信匣或設定攔截**：跟現有三支通知信一樣的已知限制，非本次 scope 要解決的問題。

## Migration Plan

不需要新 migration——`Event.host_email` 是既有欄位，建立活動時本來就會寫入。
