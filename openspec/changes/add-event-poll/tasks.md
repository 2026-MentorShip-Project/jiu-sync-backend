> TDD 排法：RED（先寫會失敗的測試）→ GREEN（只寫剛好讓它通過的最小實作）。Seam 走 HTTP 層測（DRF test client）。
>
> 這個 change 不引入新工具鏈/新依賴，不需要獨立的環境健檢 task。base 選在
> `feature/add-event-lifecycle`（已含 lifecycle+reopen+comments，即將透過
> PR#18 併回 develop），因為 `responseCount`/`latestResponseAt` 需要排除
> `ParticipantResponse.deleted_at`（取消活動時軟刪除），這個欄位是
> `add-event-lifecycle` 才新增的，`develop` 當下還沒有。
>
> 每項驗證方式前綴 `(auto)`/`(manual)`：全部是後端 API，全部可由 `pytest`/
> `manage.py` 指令自動驗證，沒有需要人工操作確認的步驟。

## 1. Seam: `ParticipantResponse.updated_at`（對應 spec 需求：改票會更新最後異動時間）

> `updated_at`（`auto_now=True`）這個欄位其實從最初的 `0003_participantresponse` migration 就存在（schema-ahead，跟 `Event.finalized_at` 等欄位同款慣例），只是從來沒有任何程式碼真的寫入過——不需要新 migration，只需要讓 `PATCH` 真的去更新它。

- [x] 1.1 [RED] 在 `apps/events/tests/test_views.py` 補測試：透過 `POST .../responses/verify` + `PATCH .../responses/{responseId}` 成功修改候選時段選擇後，直接查 DB（`ParticipantResponse.objects.get(pk=...).updated_at`）確認這個時間戳記比修改前新——確認這則測試現在是 FAIL（`updated_at` 沒被更動，等於建立時的初始值）— (auto) `pytest` 顯示 FAIL
- [x] 1.2 [GREEN] 實作：`apps/events/views.py` 的 `ParticipantResponseDetailView.patch()` 在 compare-and-swap 成功、`ParticipantResponseSlotAvailability` 重建完成後，於同一個 `transaction.atomic()` 內補一次 `ParticipantResponse.objects.filter(pk=participant_response.pk).update(updated_at=claimed_at)`（沿用已算好的 `claimed_at`，不重新呼叫 `timezone.now()`）。讓 1.1 轉綠 — (auto) `pytest` 該測試通過

## 2. Seam: `GET /api/events/{id}/poll`（對應 spec 需求：輕量輪詢端點）

- [x] 2.1 [RED] 在 `apps/events/tests/test_views.py` 補測試，涵蓋：① 有投票與留言 → 200，`responseCount`/`commentCount`/`latestResponseAt`/`latestCommentAt` 正確；② 沒有投票也沒有留言 → 200，count 皆 0、時間皆 `null`；③ 改票後 `latestResponseAt` 更新、`responseCount` 不變（依賴 Seam 1）；④ 取消活動後（既有投票軟刪除）：`responseCount` 排除被軟刪除的投票，`status`/`displayStatus` 反映 `cancelled`；⑤ 留言被軟刪除後 `commentCount` 排除、`latestCommentAt` 不算入該則；⑥ 定案/重新開放後 `status`/`displayStatus` 正確反映；⑦ 連結已失效（`cancelled_at`/`finalized_at` 超過 7 天）→ 410 `LINK_EXPIRED`；⑧ 活動不存在 → 404 `EVENT_NOT_FOUND`；⑨ 未登入（不帶 token）也能成功查詢（公開端點）。確認這組測試現在是 FAIL（路由不存在，404 Not Found）— (auto) `pytest` 顯示 FAIL
- [x] 2.2 [GREEN] 實作：`apps/events/views.py` 新增 `EventPollView`（`AllowAny` + 空 `authentication_classes`；`_get_event_or_404` 取得活動 → `compute_display_status` 算出顯示狀態，為 `link_expired` 時 `raise Gone(..., code="LINK_EXPIRED")` → 對 `ParticipantResponse`/`Comment` 各自用 `deleted_at__isnull=True` 過濾後 `.aggregate(Count("id"), Max("updated_at"/"created_at"))` → 回傳 `{status, displayStatus, responseCount, latestResponseAt, commentCount, latestCommentAt}`）；`apps/events/urls.py` 新增 `<shortid:id>/poll/` 路由。讓 2.1 全部轉綠 — (auto) `pytest apps/events/tests/test_views.py` 該檔全綠

## 3. 收尾

- [x] 3.1 全套驗證：`pytest -q`、`ruff check .`、`manage.py check`、`makemigrations --check --dry-run` 皆乾淨；`spectra validate add-event-poll --strict` 通過 — (auto)
- [x] 3.2 `/code-review`：跑一次自審，處理發現的真實問題（若有）— (auto)
