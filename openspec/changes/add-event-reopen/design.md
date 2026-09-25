## Context

`add-event-lifecycle` 已經做完 `finalize`/`cancel`，把整套「擁有者檢查優先於連結失效檢查 → compare-and-swap 狀態轉換 → 輕量 queryset 做前置檢查、重量 queryset 做最終回應 → `_schedule_notification()` 包住通知信例外」的模式踩穩、也讓 code-review 抓過一輪真實問題修正過了。這次的 `reopen` 直接沿用同一套模式，不重新發明。

`add-event-lifecycle` design.md 的 Risks 段落原本記了一條：「若未來加回 `reopen`、允許同一活動重新投票，`ParticipantResponse.unique_together (event, nickname)` 會被已軟刪除的舊紀錄擋住」——這個風險**只發生在「從 `cancelled` reopen」的情境**（因為只有 `cancel` 才會軟刪除投票資料）。使用者這次明確把 scope 限定在「已定案後重新開放」（`finalized` → `active`），`finalize` 本身完全不會軟刪除任何 `ParticipantResponse`，所以這個風險在本次 scope 內不成立，不需要處理。

## Goals / Non-Goals

**Goals:**
- 主揪可以把一筆已定案（`finalized`）的活動重新開放成進行中（`active`），帶入新的投票截止時間
- 重新開放後既有投票資料原封不動保留
- 成功後非同步通知留 Email 的參與者與主揪本人

**Non-Goals:**
- 從 `cancelled` 重新開放（使用者明確排除，`cancelled` 維持終態）
- 重新開放時一併修改候選時段（`slots`）

## Decisions

### D1. 只能從 `finalized` 重新開放，`active`/`cancelled` 皆拒絕，統一用 `EVENT_NOT_FINALIZED`

使用者明確確認（見對話："reopen 是用在我已經確定最終定案後重新開放投票"）。不像 `add-event-lifecycle` D5 需要區分 `EVENT_ALREADY_FINALIZED`/`EVENT_ALREADY_CANCELLED` 兩種 code（因為 `finalize` 有兩個不同的「不能執行」來源，語意不同）——`reopen` 只有一種允許的前置狀態（`finalized`），不管目前是 `active` 還是 `cancelled`，拒絕的底層原因都相同：「這個活動現在不是已定案狀態」，用同一個 `EVENT_NOT_FINALIZED` 就夠，不需要分兩個 code。

### D2. 請求須帶新的 `responseDeadline`，驗證規則沿用既有的 `_validate_response_deadline_in_future`

原本的 `response_deadline` 既然已經走到 `finalized`（通常代表原截止時間已過，或至少主揪已經決定不再等新投票），重新開放時沒有一個「自動延後」的合理預設值，必須由主揪明確指定新的未來時間——跟 `EventCreateSerializer`/`EventPatchSerializer` 對 `responseDeadline` 的既有驗證規則（必須晚於現在，`DEADLINE_IN_PAST`）完全一致，直接重用同一個驗證函式，不重複寫一份。

### D3. 既有參與者投票資料完全不動

`finalize` 不會軟刪除任何 `ParticipantResponse`（只有 `cancel` 才會），所以 `finalized` → `active` 這條路徑上，`ParticipantResponse` 表裡的資料本來就是完整、未被軟刪除的——`reopen` 不需要對 `ParticipantResponse` 做任何操作，`CAS UPDATE` 只需要動 `Event` 自己的欄位（`status`/`response_deadline`/`final_slot`/`final_note`/`finalized_at`）。

### D4. 通知信、CAS、輕重 queryset 分離、例外隔離——完全沿用 `add-event-lifecycle` 已修正過的模式

不重新設計：
- `EventReopenView` 比照 `EventFinalizeView`/`EventCancelView`：先用輕量 `_get_event_or_404(id)` 做擁有者檢查（優先）→ `_display_status_or_410` → CAS `UPDATE ... WHERE status='finalized'`，`affected==0` 時回 `EVENT_NOT_FINALIZED`
- 成功後重新以 `_event_with_responses_queryset()` 查一次，序列化回應
- 用既有的 `_schedule_notification(send_event_reopened_email, event.id)` 排入通知信，寄信失敗不會讓這次 API 回應失敗（`add-event-lifecycle` 2026-09-23 修訂已經處理過這個問題，這裡直接受益）
- `send_event_reopened_email` 沿用 `_notification_recipients()`（留 Email 的參與者 + 主揪本人），並在 task 執行當下重新核對 `event.status == Event.Status.ACTIVE` 才寄信（比照 `send_event_finalized_email`/`send_event_cancelled_email` 的既有守門模式）

> **code-review 補充（2026-09-24）**：抓到一個真實 bug——`EventReopenView` 原本沿用 `EventFinalizeView`/`EventCancelView` 的既有寫法，在擁有者檢查之後、CAS 之前呼叫 `_display_status_or_410(event)`。但 `reopen` 唯一的可執行前提狀態是 `finalized`，而 `finalized` 活動一旦超過 7 天（`finalized_at` 起算，不是聚會日期）就會被 `lifecycle.compute_display_status()` 算成 `link_expired`——這正是 `reopen` 最主要的使用情境（主揪很久以前定案了，現在想重開），導致這個功能對它自己的核心用途完全用不了，每次都先被 410 擋下、永遠碰不到 CAS 判斷。已用實測驗證（建一筆 8 天前定案的活動呼叫 `reopen`，修正前回 410 LINK_EXPIRED 且活動狀態不變，修正後回 200 且成功轉為 `active`）。
>
> 修法：`EventReopenView` 不再呼叫 `_display_status_or_410`。連結是否失效是給參與者這類公開／匿名端點用的「這個連結已經死了、別再互動」概念，不該套用在主揪對自己活動的生命週期管理動作上。
>
> 順帶發現並修正同款問題也存在於 `add-event-lifecycle` 已經寫好的 `EventCancelView`——`cancel` 的可執行前提狀態包含 `finalized`，同樣會被「定案超過 7 天」擋成 410，導致主揪永遠無法取消一筆「已經定案一段時間、但聚會可能還沒發生」的活動；一併拿掉那支的 `_display_status_or_410` 呼叫。`EventFinalizeView`（可執行前提只有 `active`，`active` 狀態本身不可能算出 `link_expired`）也一併拿掉同一行呼叫，理由是保持三支端點行為一致、不留下「只是恰好沒觸發」的隱性依賴，不是修正真正的行為問題（這支本來就沒有這個 bug）。三支的既有「已取消／已定案」狀態衝突測試（原本測「連結已失效」情境）改為驗證：即使活動連結已失效，回應仍是語意化的 409（`EVENT_ALREADY_CANCELLED`/`EVENT_ALREADY_FINALIZED`/`EVENT_NOT_FINALIZED`），不是 410——這是修正後的預期行為，不是回歸。新增兩則直接驗證這次 bug 修正的回歸測試（`test_owner_can_reopen_event_finalized_more_than_7_days_ago`、`test_owner_can_cancel_event_finalized_more_than_7_days_ago`）。

## Risks / Trade-offs

- **[風險] 同一活動可以被 finalize → reopen → finalize → reopen 無限次來回** → 沒有次數限制，也沒有歷史記錄（每次 `finalize` 覆蓋前一次的 `final_slot`/`final_note`/`finalized_at`）。這是既有 `finalize`/`cancel` 也共有的性質（狀態機沒有版本歷史），非本次新增風險，不特別處理。
- **[風險] `reopen` 沒有限制「原本 `finalNote`/`final_slot` 選了什麼」，重新開放後這些資訊直接消失** → 主揪若需要保留「上次定案是選哪個時段」的記錄，目前沒有任何地方存這個歷史。非本次 scope，若未來需要可以另外加一個 `EventLifecycleHistory` 之類的稽核表。

## Migration Plan

不需要新 migration——`Event.status`/`response_deadline`/`final_slot`/`final_note`/`finalized_at` 全部是既有欄位，這次只是新增一條允許的狀態轉換路徑。
