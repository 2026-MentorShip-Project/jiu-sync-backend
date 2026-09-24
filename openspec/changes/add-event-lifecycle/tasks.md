> TDD 排法：RED（先寫會失敗的測試）→ GREEN（只寫剛好讓它通過的最小實作）。Seam 走 HTTP 層測（DRF test client）。
>
> 這個 change 首次真正串接既有但從未被呼叫過的 Celery＋Mailers 設定——雖然 Celery 本身不是新依賴（`pyproject.toml`/`config/celery.py` 早就有），但「這條路徑第一次真的被執行」帶有不小的環境風險（Django 6.1 `MAILERS` API、`CELERY_TASK_ALWAYS_EAGER`、`django_capture_on_commit_callbacks` 這幾個都還沒在專案裡用過），比照 CLAUDE.md「新工具鏈或新依賴需要環境健檢 task」的精神，第一個 task 先做一個最小可行的健檢，確認整條路徑（設定→task→寄信→測試斷言）真的走得通，再疊上 finalize/cancel 的業務邏輯。
>
> 每項驗證方式前綴 `(auto)`/`(manual)`：全部是後端 API，全部可由 `pytest`/`ruff`/`manage.py` 指令自動驗證，沒有需要人工操作確認的步驟。

## 0. 環境健檢：Celery + Mailers 首次真正串接

- [x] 0.1 [RED] `config/settings/base.py` 新增 `CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)`；`config/settings/dev.py` 覆寫預設為 `True`（含 `CELERY_TASK_EAGER_PROPAGATES = True`，讓測試裡 task 拋例外會真的讓測試失敗，不會被吞掉）。新增 `apps/notifications/tasks.py`：先放一個最小的 `@shared_task def _healthcheck_send_test_email(to_email): send_mail(...)`。補一個健檢測試：呼叫 `.delay()` 後（搭配 `django_capture_on_commit_callbacks` 或直接呼叫，因為這支不牽涉 `on_commit`）用 `mailoutbox` fixture 斷言收到一封信。確認 FAIL（task 還不存在）— (auto) `pytest` 顯示 FAIL
- [x] 0.2 [GREEN] 實作到讓 0.1 轉綠 — (auto) `pytest` 該測試通過，之後任何時候可以刪掉這個健檢 task/測試（僅用來驗證管線，不是正式功能），實際留存與否視最終是否被下方 Seam 的正式 task 取代而定

## 1. Seam: `POST /api/events/{id}/finalize`（對應 spec 需求：主揪定案活動）

- [x] 1.1 [RED] 新增 `EventFinalizeSerializer`（`finalSlotId`/`finalNote`，`validate_finalSlotId` 確認屬於該活動候選時段，context 帶入 `event`）。`apps/notifications/tasks.py` 新增 `send_event_finalized_email(event_id)`（查 `event.responses` 裡有 Email 的 + `host_email`，寄一封純文字通知信，內容含活動標題／定案時段／備註／分享連結）。`config/exceptions.py` 的 `FIELD_CODE_OVERRIDES` 補上 `finalSlotId`/`finalNote` 的 code。在 `apps/events/tests/test_views.py` 補測試，涵蓋：① 合法 `finalSlotId`（＋選填 `finalNote`）→ 200，回應為完整活動格式，`status="finalized"`，DB 該活動的 `finalized_at`/`final_slot`/`final_note` 正確寫入；② 非擁有者（已登入）→ 403 `FORBIDDEN`，活動不受影響；③ 未登入 → 401；④ `finalSlotId` 不屬於該活動 → 400 `SLOT_NOT_FOUND`；⑤ `finalSlotId` 缺漏 → 400 對應 code；⑥ `finalNote` 超過 200 字 → 400 對應 code；⑦ 活動已經是 `finalized` → 409 `EVENT_ALREADY_FINALIZED`；⑧ 活動已經是 `cancelled` → 409 `EVENT_ALREADY_CANCELLED`；⑨ 活動連結已失效 → 410 `LINK_EXPIRED`；⑩ 活動不存在 → 404 `EVENT_NOT_FOUND`；⑪ 定案成功後，用 `django_capture_on_commit_callbacks`＋`mailoutbox` 斷言留 Email 的參與者與主揪本人各收到一封通知信，沒留 Email 的參與者不會收到。確認這組測試現在是 FAIL — (auto) `pytest` 顯示 FAIL
- [x] 1.2 [GREEN] 實作：`apps/events/views.py` 新增 `EventFinalizeView`（`IsAuthenticated`，先 `_get_event_or_404`/`_display_status_or_410`，比對擁有者，`EventFinalizeSerializer` 驗證，`Event.objects.filter(pk=event.id, status=Event.Status.ACTIVE).update(...)` compare-and-swap 寫入 `status`/`final_slot`/`final_note`/`finalized_at`，`affected==0` 時查目前狀態決定回 `EVENT_ALREADY_FINALIZED` 或 `EVENT_ALREADY_CANCELLED`，成功則重新以 `_event_with_responses_queryset()` 查一次、`transaction.on_commit()` 排入通知信 task，回傳完整 `EventDetailSerializer`）；`apps/events/urls.py` 新增路由。讓 1.1 全部轉綠為止 — (auto) `pytest apps/events/tests/test_views.py` 該檔全綠

## 2. Seam: `POST /api/events/{id}/cancel`（對應 spec 需求：主揪取消活動、查詢單一活動完整資料排除軟刪除投票）

- [x] 2.1 [RED] `ParticipantResponse` 新增 `deleted_at`（nullable `DateTimeField`），跑 `makemigrations`。`_event_with_responses_queryset()` 的 `responses` prefetch 改用 `Prefetch("responses", queryset=ParticipantResponse.objects.filter(deleted_at__isnull=True).prefetch_related("slot_availabilities"))`（design.md D6）。`apps/notifications/tasks.py` 新增 `send_event_cancelled_email(event_id)`（`event.responses.all()`，刻意不過濾 `deleted_at`，見 design.md D2a）。補測試：① 主揪成功取消進行中且已有投票的活動 → 200，`status="cancelled"`，DB 該活動全部 `ParticipantResponse.deleted_at` 皆非空，`GET /api/events/{id}` 的 `responses`/`slotSummary` 反映為空／全 0；② 主揪成功取消已定案的活動 → 200；③ 非擁有者（已登入）→ 403 `FORBIDDEN`；④ 未登入 → 401；⑤ 活動已經是 `cancelled` → 409 `EVENT_ALREADY_CANCELLED`，投票資料不受影響（`deleted_at` 維持原狀，不重複標記）；⑥ 活動連結已失效 → 410 `LINK_EXPIRED`；⑦ 活動不存在 → 404 `EVENT_NOT_FOUND`；⑧ 取消不影響既有留言（`GET /api/events/{id}/comments` 仍看得到）；⑨ 取消成功後，用 `django_capture_on_commit_callbacks`＋`mailoutbox` 斷言原本留 Email 的參與者（即使投票已被軟刪除）與主揪本人各收到一封取消通知信；⑩ 兩個並發取消請求同一活動，用真實 thread（`@pytest.mark.django_db(transaction=True)`）驗證只有一個成功（200），另一個 409（比照 `ParticipantResponseDetailView.patch()`/`CommentDetailView.delete()` 的既有併發測試寫法，design.md D7）。確認這組測試現在是 FAIL — (auto) `pytest` 顯示 FAIL
- [x] 2.2 [GREEN] 實作：`apps/events/views.py` 新增 `EventCancelView`（結構同 `EventFinalizeView`，CAS 條件為 `status__in=[Event.Status.ACTIVE, Event.Status.FINALIZED]`，成功時同一個 `transaction.atomic()` 內用 `ParticipantResponse.objects.filter(event=event, deleted_at__isnull=True).update(deleted_at=<CAS 用的同一個 now>)` 批次軟刪除，`affected==0` 時固定回 `EVENT_ALREADY_CANCELLED`）；`apps/events/urls.py` 新增路由。讓 2.1 全部轉綠為止 — (auto) `pytest apps/events/tests/test_views.py` 該檔全綠

## 3. 收尾

- [x] 3.1 全套驗證：`pytest -q`、`ruff check .`、`manage.py check`、`makemigrations --check --dry-run` 皆乾淨；`openspec validate add-event-lifecycle --strict` 通過 — (auto)
- [x] 3.2 `/code-review`：跑一次自審，處理發現的真實問題（若有）— (auto)。發現
  六個問題，四個真的修（取消已定案活動未清 final_slot/final_note/
  finalized_at；owner 檢查順序跟連結失效檢查顛倒；兩個通知信 task 沒有在
  執行當下重新核對活動狀態；兩個 view 寫入前檢查用了不必要的重量
  queryset），一個記成接受的技術債（四處重複的 CAS 手刻邏輯，不抽共用
  helper），一個是過時註解一併訂正。詳見 design.md code-review 補充
- [x] 3.3 回傳完整的錯誤代碼對照給使用者（本輪新增 `EVENT_ALREADY_FINALIZED`/`EVENT_ALREADY_CANCELLED`/`SLOT_NOT_FOUND`(沿用)/`finalSlotId`,`finalNote` 相關 code），比照本 session 先前 `add-participant-responses` 階段做過的同款輸出 — (manual，僅是聊天室輸出，不是文件變更)
- [x] 3.4 使用者實機測試 `finalize` 時發現：`.env` 未設定 `EMAIL_BACKEND`
  導致 `send_mail()` 對 console backend 傳入不認得的 SMTP `OPTIONS`，拋
  `InvalidMailer`；本機 `CELERY_TASK_ALWAYS_EAGER=True` 讓這個例外同步炸穿
  `EventFinalizeView`，回應變成 500——但用 `dbshell` 實測確認定案本身（DB
  寫入）已經 commit 成功，不是原子性問題，是通知信失敗被誤當成整個請求失
  敗。新增 `_schedule_notification()` 包住 `on_commit` callback 的例外
  （`try/except` + `logger.exception`），`EventFinalizeView`/
  `EventCancelView` 改用這個共用函式；補回歸測試（`monkeypatch` 讓
  `.delay()` 拋例外，斷言 API 仍回 200 且狀態正確轉換），RED→GREEN 驗證
  過。見 design.md Risks 段落 2026-09-23 修訂記錄、incident-log 同日條目
  — (auto) `pytest -q`（全套 210 passed）、`ruff check .`、`manage.py
  check` 皆乾淨

## 4. `finalAttendees` 欄位（D9，2026-09-24 追加，使用者實測發現前端需求）

- [x] 4.1 [RED] `apps/events/tests/test_views.py` 補
  `test_finalized_event_finalAttendees_only_lists_available_for_final_slot`
  （3 位參與者，1 位對定案時段表態 available、1 位 if_needed、1 位只對另一
  時段 available，斷言 `finalAttendees` 只含第一位，欄位為
  `{id, nickname, comment}`）與
  `test_active_event_finalAttendees_is_empty_list`（未定案時為 `[]`，不是
  `null`）；同步更新既有 D16 完整欄位集合斷言加入 `finalAttendees` — (auto)
  `pytest -k finalAttendees` 顯示 FAIL（`KeyError: 'finalAttendees'`）
- [x] 4.2 [GREEN] `apps/events/serializers.py` 的 `EventDetailSerializer`
  新增 `finalAttendees = serializers.SerializerMethodField()`，
  `get_finalAttendees()` 在 `event.final_slot_id` 為空時回傳 `[]`，否則走
  已 prefetch 的 `event.responses.all()`/`slot_availabilities.all()`
  過濾出對定案時段表態 `available` 的參與者 — (auto)
  `pytest -k finalAttendees` 全綠，`pytest apps/events/ -q`（169 passed）
- [x] 4.3 `openspec/changes/add-event-lifecycle/specs/events/spec.md` 的
  「查詢單一活動完整資料」Requirement 加修訂記錄 + 2 個 Scenario；
  design.md 補 D9 — (auto) `openspec validate add-event-lifecycle --strict`
