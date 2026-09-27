## Why

留言板（`POST /api/events/{id}/comments`）完全公開、免登入，暱稱不驗證身分（跟 `ParticipantResponse` 已經有手機末三碼驗證不同）。任何人可以用腳本或連點對同一活動洗版留言。需要一個輕量的防洗版機制。

## What Changes

- `POST /api/events/{id}/comments` 新增 Redis TTL 鎖：留言驗證通過、真的寫入 DB 成功後，以「來源 IP + 活動 id」為 key 上鎖 2 秒
- 鎖定期間內，同一來源 IP 對**同一活動**再次留言，系統拒絕請求，回傳 429，錯誤代碼 `COMMENT_RATE_LIMITED`
- 新增 client IP 擷取 helper：信任 nginx 轉發的 `X-Forwarded-For`（取第一個值），本機開發環境（無 nginx）退回 `REMOTE_ADDR`
- Redis 連線失敗時 fail-open：跳過鎖定檢查，留言正常寫入，只記 log，不阻擋留言功能本身

未涵蓋：跨活動的全域限制（只鎖同一活動，不同活動的鎖互相獨立）；除留言以外的其他端點防洗版（例如投票）；封鎖／黑名單機制（只是 2 秒的時間窗口鎖，不是永久封鎖）。

## Capabilities

### New Capabilities

（無 — 沿用既有 `events` capability）

### Modified Capabilities

- `events`：MODIFIED Requirement「新增活動留言」——補上短時間內重複留言的防洗版限制

## Impact

- 修改 `apps/events/views.py`：`CommentListCreateView.post()`
- 新增 client IP 擷取 helper
- 新增獨立於 Celery broker 的 Redis 連線設定（`COMMENT_RATE_LIMIT_REDIS_URL`，同一 Redis instance 的不同 DB index）
- 不需要新 migration
