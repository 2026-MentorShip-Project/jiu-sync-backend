> TDD 排法：RED（先寫會失敗的測試）→ GREEN（只寫剛好讓它通過的最小實作）。Seam 走 HTTP 層測（DRF test client）。
>
> 這個 change 完全沿用 `add-event-lifecycle` 已經驗證過、且被 code-review 修正過的模式（擁有者檢查優先、CAS、輕重 queryset 分離、`_schedule_notification` 例外隔離），不引入新工具鏈，不需要獨立的環境健檢 task。
>
> 每項驗證方式前綴 `(auto)`/`(manual)`：全部是後端 API，全部可由 `pytest`/`ruff`/`manage.py` 指令自動驗證，沒有需要人工操作確認的步驟。

## 1. Seam: `POST /api/events/{id}/reopen`（對應 spec 需求：主揪重新開放投票）

- [x] 1.1 [RED] 新增 `EventReopenSerializer`（`responseDeadline`，`validate_responseDeadline` 重用既有 `_validate_response_deadline_in_future`）。`apps/notifications/tasks.py` 新增 `send_event_reopened_email(event_id)`（比照 `send_event_finalized_email`/`send_event_cancelled_email`：查 `event.status == Event.Status.ACTIVE` 才寄，`_notification_recipients()` 找收件人）。`config/exceptions.py` 不需要新的 `FIELD_CODE_OVERRIDES`（`responseDeadline` 沿用既有的 `RESPONSE_DEADLINE_REQUIRED`；`DEADLINE_IN_PAST` 是手動 `raise ValidationError(..., code=...)`，不經過對照表）。在 `apps/events/tests/test_views.py` 補測試，涵蓋：① 主揪成功重新開放已定案活動 → 200，回應 `status="active"`、新 `responseDeadline`、`finalSlotId`/`finalNote` 皆為 `null`，DB 正確寫入，既有投票紀錄不受影響（`responses`/`slotSummary` 內容不變）；② 已登入但非擁有者 → 403 `FORBIDDEN`；③ 未登入 → 401；④ 新截止時間早於/等於現在 → 400 `DEADLINE_IN_PAST`；⑤ 新截止時間缺漏 → 400 `RESPONSE_DEADLINE_REQUIRED`；⑥ 對 `active` 活動重新開放 → 409 `EVENT_NOT_FINALIZED`；⑦ 對 `cancelled` 活動重新開放 → 409 `EVENT_NOT_FINALIZED`；⑧ 活動連結已失效 → 410 `LINK_EXPIRED`；⑨ 活動不存在 → 404 `EVENT_NOT_FOUND`；⑩ 重新開放成功後，用 `django_capture_on_commit_callbacks`＋`mailoutbox` 斷言留 Email 的參與者與主揪本人各收到一封通知信；⑪ 通知信 `.delay()` 拋例外時 API 仍回 200（比照 `add-event-lifecycle` 2026-09-23 修訂的既有回歸測試寫法）；⑫ 兩個並發重新開放請求同一活動，只有一個成功、另一個 409（比照既有併發測試寫法）。確認這組測試現在是 FAIL — (auto) `pytest` 顯示 FAIL
- [x] 1.2 [GREEN] 實作：`apps/events/views.py` 新增 `EventReopenView`（結構同 `EventFinalizeView`：輕量 `_get_event_or_404(id)` → 擁有者檢查 → `_display_status_or_410` → `EventReopenSerializer` 驗證 → `Event.objects.filter(pk=event.id, status=Event.Status.FINALIZED).update(status=Event.Status.ACTIVE, response_deadline=..., final_slot=None, final_note=None, finalized_at=None)`，`affected==0` 回 `EVENT_NOT_FINALIZED`，成功後重新查重量 queryset、`_schedule_notification(send_event_reopened_email, event.id)`，回傳完整 `EventDetailSerializer`）；`apps/events/urls.py` 新增 `<shortid:id>/reopen/` 路由。讓 1.1 全部轉綠為止 — (auto) `pytest apps/events/tests/test_views.py` 該檔全綠

## 2. 收尾

- [x] 2.1 全套驗證：`pytest -q`、`ruff check .`、`manage.py check`、`makemigrations --check --dry-run` 皆乾淨；`openspec validate add-event-reopen --strict` 通過 — (auto)
- [x] 2.2 `/code-review`：跑一次自審，處理發現的真實問題（若有）— (auto)。發現
  一個真實 bug 並修正：`EventReopenView` 呼叫 `_display_status_or_410`
  導致對它最主要的使用情境（定案超過 7 天想重開）永遠回 410，功能形同
  無法使用；同款問題也存在於既有的 `EventCancelView`，一併修正
  （`EventFinalizeView` 為求一致也拿掉同一行，但那支本來就不會觸發）。
  補上兩則直接驗證此次修正的回歸測試，既有「連結已失效」測試改為驗證
  正確的語意化 409。詳見 design.md 2026-09-24 修訂、incident-log 同日
  條目
