> TDD 排法：RED（先寫會失敗的測試）→ GREEN（只寫剛好讓它通過的最小實作）。Seam 走 HTTP 層測（DRF test client）+ task 直接呼叫測（比照 `send_event_finalized_email` 等既有三支的測法）。
>
> 這個 change 不引入新工具鏈/新依賴，直接複用既有 `_schedule_notification`/`_event_share_url`/`_send_to_each_recipient`，不需要獨立的環境健檢 task。

## 1. Seam: `send_event_created_email` task（對應 spec 需求：建立活動成功後寄送分享連結通知信）

- [ ] 1.1 [RED] 在 `apps/notifications/tests/test_tasks.py`（或既有對應測試檔）補測試：① `event.host_email` 存在時，task 執行後 `django.core.mail.outbox` 收到一封信、收件人為 `host_email`、內容含 `_event_share_url(event)`；② `event.host_email` 為 `None`/空字串時不寄信；③ `event_id` 對應不到任何 `Event` 時（防禦性）不拋例外、不寄信。確認這組測試現在 FAIL（task 不存在）— (auto) `pytest` 顯示 FAIL
- [ ] 1.2 [GREEN] 實作：`apps/notifications/tasks.py` 新增 `send_event_created_email(event_id)`（`@shared_task`），比照既有三支的寫法：查 `Event`、`host_email` 為空則 return、組信件內容（含 `_event_share_url`）、寄出。讓 1.1 全部轉綠 — (auto) `pytest` 該檔全綠

## 2. Seam: `EventCreateView.post()` 排程通知信（對應同一條 spec 需求）

- [ ] 2.1 [RED] 在 `apps/events/tests/test_views.py` 補測試：`POST /api/events` 成功建立活動（`CELERY_TASK_ALWAYS_EAGER=True` 測試環境）後，`mail.outbox` 收到一封主旨/內容含活動標題與分享連結的信、收件人為登入使用者的 email。確認這組測試現在 FAIL（尚未排程任何通知信）— (auto) `pytest` 顯示 FAIL
- [ ] 2.2 [GREEN] 實作：`EventCreateView.post()` 在 `serializer.save(...)` 成功後，呼叫 `_schedule_notification(send_event_created_email, event.id)`。讓 2.1 轉綠 — (auto) `pytest apps/events/tests/test_views.py` 該檔全綠

## 3. 收尾

- [ ] 3.1 全套驗證：`pytest -q`、`ruff check .`、`manage.py check`、`makemigrations --check --dry-run`（預期無變化）皆乾淨；`spectra validate add-event-created-email --strict` 通過 — (auto)
- [ ] 3.2 `/code-review`：跑一次自審，處理發現的真實問題（若有）— (auto)
