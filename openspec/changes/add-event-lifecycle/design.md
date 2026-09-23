## Context

`Event.status`（`active`/`finalized`/`cancelled`）與 `finalized_at`/`cancelled_at`/`final_slot`/`final_note` 四個欄位在 `add-events-api` 就已經「schema-ahead」建好了（見 `apps/events/models.py::Event` docstring：「deliberate schema-ahead fields so a future finalize/cancel feature doesn't need its own migration」），`compute_display_status()` 也已經是純函式、吃這些欄位算出六態 `displayStatus`，docstring 明講是給「future finalize/cancel...code」重用。這次要做的，純粹是把「誰、在什麼條件下，可以把這些欄位改成什麼值」這件事用 API 補上——不需要新的顯示狀態邏輯，也不需要新的 `Event` schema 欄位（`ParticipantResponse` 除外，見 D6）。

`config/settings/base.py` 也已經預留 Celery（broker=Redis，`docker-compose.yml` 已有 `redis` 服務）＋ Django 6.1 的 `MAILERS` 設定，註解寫明是給「finalized / deadline / board update」這類通知用的。但目前專案裡沒有任何一個地方真的呼叫過 `.delay()` 或 `send_mail()`——`apps/notifications` 這個 app 存在、註冊在 `INSTALLED_APPS`，但四個檔案都是 `startapp` 產生的空殼，從沒被實作過。這是這兩支端點首次真正把這條已規劃但沒人用過的管線接起來。

## Goals / Non-Goals

**Goals:**
- 主揪可以定案（選最終時段）、取消整場活動
- 定案／取消成功後，非同步寄送通知信給留過 Email 的參與者與主揪本人
- 取消活動時，既有投票資料软刪除，不再出現在查詢結果中

**Non-Goals:**
- 重新開放投票（`reopen`）——本次明確排除，見 proposal.md
- Email 樣式、多語系、寄送失敗重試機制
- 留言板行為改動（維持 `add-event-comments` D5：活動狀態不影響留言）

## Decisions

### D1. Email 寄送：現在就真的接 Celery 非同步寄送，不 stub

已與使用者確認（grill-me）：MVP 階段直接把 `config/settings/base.py` 早就預留的 Celery＋Mailers 接起來，不是先做一個假的寄送介面。新增 `apps/notifications/tasks.py` 放 `@shared_task` 的通知信 task（延續 app 本身「notifications」的職責分工，不塞進 `apps/events`）。

寄送時機用 `transaction.on_commit(lambda: task.delay(event.id))`，不是在請求處理當下直接呼叫——task 傳 `event_id`（純值），不是傳整個 model instance，避免 worker 執行時撈到的是序列化當下的舊物件；`on_commit` 確保只有在這次狀態轉換真的寫進 DB 之後，通知信才會被排入佇列，不會因為交易稍後 rollback 而寄出一封對應不到任何真實狀態變化的信。

### D2. Celery 執行模式：`dev`/測試環境預設 `CELERY_TASK_ALWAYS_EAGER = True`，`prod` 維持真正非同步

本機開發與測試環境沒有跑 Celery worker（`docker-compose.yml` 只有 `redis`，沒有 worker 服務），`CELERY_TASK_ALWAYS_EAGER = True` 讓 `.delay()` 在呼叫當下同步執行完，不需要另外啟動 worker 就能跑通整條路徑（包含測試斷言寄出的信）。`prod.py` 不覆寫，沿用 `base.py` 的預設 `False`——正式環境需要真正部署一個 Celery worker 程序，這是部署面的獨立工作，不在本次 scope（比照 `add-participant-responses`/`add-event-comments` 對「部署環境設置」一貫不在 API 開發 change 裡處理的做法）。

測試斷言用 pytest-django 內建的 `mailoutbox` fixture（背後是 Django `locmem` mailer——`django.test.utils.setup_test_environment()` 本來就會自動把 `MAILERS` 的每個 alias 換成 `locmem`，不需要額外設定）搭配 `django_capture_on_commit_callbacks` fixture（`pytest-django>=4.4` 內建，讓測試裡的 `transaction.on_commit()` 回呼真的被執行，不用像先前併發測試那樣另開真實 thread）。

### D2a. Email 通知涵蓋對象：留過 Email 的參與者 + 主揪本人，不篩選已軟刪除

已與使用者確認（grill-me 前置提問已在 proposal.md 說明）：收件人是 `event.responses` 裡 `email` 不為空的每一筆，加上 `event.host_email`（若有填寫）。**刻意不過濾 D6 的軟刪除狀態**——`cancel` 端點是先軟刪除既有投票資料、才觸發通知信，等 Celery task 真正執行時查詢 `event.responses`，這些紀錄已經是「已軟刪除」，若這裡也 `filter(deleted_at__isnull=True)` 會查到空結果、通知信永遠寄不出去，跟需求「取消後要通知曾經投過票的人」矛盾。`finalize` 端點不涉及軟刪除，`.all()` 跟過濾後結果一致，用同一支不分情境的查詢寫法更簡單。

### D3. `POST /finalize`：僅 `active` 狀態可執行，成功轉為 `finalized`

`finalSlotId` 必填，須為該活動 `slots` 其中之一，否則沿用既有的 `SLOT_NOT_FOUND`（跟參與者投票驗證候選時段的既有 code 一致，語意相同：「這個 slot id 不屬於這個活動」）。`finalNote` 選填，上限 200 字，比照 `ParticipantResponse.comment`（`add-participant-responses` D12）同款慣例：空字串正規化成 `None`，不是驗證錯誤。

狀態衝突：已與使用者確認（grill-me）三支端點（原規劃含 `reopen`，現只剩 `finalize`/`cancel`）各自的狀態衝突用獨立語意化 code，不沿用既有 `EVENT_NOT_ACTIVE`：

- 活動已經是 `finalized` → 409 `EVENT_ALREADY_FINALIZED`
- 活動已經是 `cancelled` → 409 `EVENT_ALREADY_CANCELLED`（見 D5，跟 `cancel` 端點共用同一個 code）

### D4. `POST /cancel`：`active` 或 `finalized` 狀態皆可執行，成功轉為 `cancelled`

已與使用者確認（grill-me）：已定案的活動仍可以取消（例如定案後才發現場地取消，需要整場取消）——沒有 `reopen` 之後，`cancelled` 是終態，但通往 `cancelled` 的入口不侷限於 `active`。唯一擋下的是「已經是 `cancelled`」的重複取消請求。

### D5. `finalize`／`cancel` 的「已取消」衝突共用同一個 `EVENT_ALREADY_CANCELLED` code

已與使用者確認（grill-me）：`finalize` 端點遇到 `cancelled` 活動、跟 `cancel` 端點遇到「已經取消過」，底層是同一個原因（活動處於取消狀態），沒必要在兩個端點各自造一個新字串。

### D6. 取消活動時，既有 `ParticipantResponse` 軟刪除（新增 `deleted_at`）

已與使用者確認（grill-me）：不是保留不動、也不是物理刪除，而是軟刪除——新增 `deleted_at`（nullable `DateTimeField`），欄位命名跟本 app 既有的 `Comment.deleted_at`（`add-event-comments` D9）一致，查詢時都是 `filter(deleted_at__isnull=True)` 的同一套慣例，不用記兩套命名。

`EventDetailSerializer.get_responses()`/`get_slotSummary()` 讀的是 `event.responses.all()`，走 `_event_with_responses_queryset()` 的 prefetch cache（見 D17/`add-participant-responses`）——要讓取消後的查詢結果排除軟刪除資料，必須把過濾條件做進 prefetch 本身，改用 `django.db.models.Prefetch`：

```python
Prefetch(
    "responses",
    queryset=ParticipantResponse.objects.filter(deleted_at__isnull=True).prefetch_related(
        "slot_availabilities"
    ),
)
```

單純把 `event.responses.filter(...)` 加在 serializer 端不會用到 prefetch cache（Django 只有原封不動的 `.all()` 才吃快取，任何額外 `.filter()` 都會另開一條新 query，等於白做 N+1 防護）——這個地雷跟 `add-participant-responses` D17 埋的 `_event_with_responses_queryset()` 是同一份程式碼，這次直接在那裡改，不動 `get_responses()`/`get_slotSummary()` 本身。

其他讀取 `ParticipantResponse` 的既有呼叫點（`ParticipantResponseVerifyView`/`ParticipantResponseDetailView` 的暱稱查找、`ParticipantResponseCreateSerializer.create()` 的碰撞判斷）刻意不追加 `deleted_at` 過濾——這次軟刪除只會在活動被取消時觸發，而活動一旦 `cancelled`，這三支端點共用的 `_check_participation_preconditions()` 早就先擋下 409 `EVENT_NOT_ACTIVE`，根本到不了那些查找邏輯，不需要重複防禦。

### D7. 併發安全：`finalize`/`cancel` 的狀態轉換皆用 compare-and-swap

比照本 session 已經處理過兩次的同款問題（`ParticipantResponseDetailView.patch()` 的 token 消費、`CommentDetailView.delete()` 的軟刪除）——主揪連點兩下「定案」或「取消」按鈕、或兩個分頁同時操作，純粹的「先查狀態、再 `.save()`」會讓兩個並發請求都通過檢查、都成功寫入。這次直接在第一版就用 `Event.objects.filter(pk=event.id, status=<允許的前置狀態>).update(...)`，靠 `UPDATE` 實際影響的 row 數判斷輸贏，不等 code-review 抓到才修（`CLAUDE.md`「資料操作穩健性規範」第6點本來就要求併發安全要主動考慮，不是被動等審查抓）。

CAS 成功後，序列化用的 `event` 物件需要反映寫入後的最新狀態——直接重新以 `_event_with_responses_queryset()` 查一次（`cancel` 還需要讓 `responses` 的 prefetch 快取反映剛剛軟刪除的結果，見 D6），不手動修補記憶體裡舊物件的個別欄位，避免漏改欄位、多一次 query 換取正確性與簡單。

### D8. 權限檢查沿用既有的 `FORBIDDEN`／401 慣例

`finalize`/`cancel` 的擁有者權限檢查比照 `EventDetailView.patch()`：已登入但非擁有者 → 403 `FORBIDDEN`（通用 code，不配專屬字串，見 `add-events-api` 的既有 grill-me 決策）；未登入 → 401。這點沒有分歧，未特別詢問即採用。

> **code-review 補充**：發現並修正四個真實問題——① `EventCancelView` 取消一筆已定案的活動時，原本沒有清空 `final_slot`/`final_note`/`finalized_at`，導致 `status="cancelled"` 卻仍回傳舊的 `finalSlotId`/`finalNote`，前端會看到自相矛盾的畫面，已在同一個 CAS `UPDATE` 裡一併清空；② `EventFinalizeView`/`EventCancelView` 原本先做連結失效檢查（`_display_status_or_410`）才做擁有者檢查，跟本節開頭講的「比照 `EventDetailView.patch()`」矛盾（`patch()` 其實是擁有者檢查優先，根本不呼叫連結失效檢查），已調整成擁有者檢查優先；③ 兩個通知信 Celery task 原本沒有在真正執行時重新核對活動狀態，`on_commit` 排入佇列後、task 真正執行前活動若已被後續請求改成別的狀態，可能寄出內容矛盾的信，已補上狀態守門；④ 兩個 view 原本用重量 `_event_with_responses_queryset()` 做寫入前的擁有者/狀態檢查，成功路徑因此白白多付一次 prefetch 成本，已改用輕量 `_get_event_or_404(id)` 做檢查、只在最終序列化回應時才查一次重量 queryset（比照 `ParticipantResponseCreateView` 既有的輕重分離寫法）。
>
> **接受的技術債（非阻斷性，不在本次處理）**：`EventFinalizeView`/`EventCancelView`/`CommentDetailView.delete()`/`ParticipantResponseDetailView.patch()` 現在有四份幾乎一樣的「compare-and-swap `UPDATE` → 檢查 `affected==0` → 回 409/404」手刻邏輯，沒有共用的抽象。code-review 建議抽成共用 helper，避免未來第五個端點（例如真的做 `reopen` 時）又手刻一次同款的併發安全問題。這次評估後選擇先不做：抽象需要涵蓋四種不同的 model／欄位組合，設計一個好用的泛型 helper 本身有不小成本，跟「先讓四個端點各自正確」比起來優先度較低，留給日後真的要新增第五個同類端點時再一併處理。

## Risks / Trade-offs

- **[風險] `ParticipantResponse` 軟刪除後，`unique_together (event, nickname)` 仍然生效** → 若未來加回 `reopen`、允許同一活動重新投票，同暱稱的舊（已軟刪除）紀錄仍會擋掉新投票的 `nickname` 唯一性——這次沒有 `reopen`，不影響本次範圍，留給未來若真的要做 `reopen` 時一併處理（可能需要把 `unique_together` 改成只在 `deleted_at IS NULL` 時生效的 partial unique index）。
- **[風險] Celery `CELERY_TASK_ALWAYS_EAGER=True` 只在 `dev`/測試環境，正式環境部署 worker 是否確實運作不在本次驗證範圍** → 本地測試只能證明「task 邏輯本身正確」，不能證明「prod 真的有 worker 在消費佇列」，這是部署面的既有落差（比照 `add-participant-responses` 對 Swagger/前端同步的處理方式：本次不處理，記錄下來）。
- **[風險] Email 寄送失敗（收件人地址無效、SMTP 逾時等）不會讓 API 請求本身失敗**（`on_commit` 之後才排入佇列，跟請求回應完全脫鉤）→ 這是刻意的設計（定案/取消動作本身的成功不應該被一封信寄不寄得出去卡住），但代表寄信失敗目前沒有任何重試或告警機制，非本次 scope。

## Migration Plan

新增 migration：`ParticipantResponse.deleted_at`（nullable `DateTimeField`，單純 `AddField`，不需要像 `add-participant-responses` D15 那樣處理已套用 migration 的本地 DB 修復問題——這次的分支是全新開的，尚未在任何地方跑過 migrate）。不修改既有 `Event`/`Slot`/`Comment` schema。
