> TDD 排法：RED（先寫會失敗的測試）→ GREEN（只寫剛好讓它通過的最小實作）。Seam 走 HTTP 層測（DRF test client），Redis 互動用測試環境的真實 Redis（比照專案既有 Celery 測試慣例，不 mock）。
>
> 實作前置條件（環境健檢，見「Task 拆分規則」——引入新設定但沒有新工具鏈/新依賴，輕量健檢即可）：確認 `COMMENT_RATE_LIMIT_REDIS_URL` 在 `config/settings/dev.py`/測試環境可以正確連上既有 docker-compose 的 `redis` service（不同 db index）。

## 0. 環境健檢

- [x] 0.1 新增 `COMMENT_RATE_LIMIT_REDIS_URL` setting（`config/settings/base.py`，dev 預設把 `CELERY_BROKER_URL` 的 db index 由 0 換成 1），確認 `redis.Redis.from_url(settings.COMMENT_RATE_LIMIT_REDIS_URL).ping()` 在本機開發環境（既有 docker-compose 的 `redis` service）回傳成功 — (auto) 手動跑一次確認,不需要寫成測試

## 1. Seam: client IP 擷取 helper（對應 spec 需求：新增活動留言——防洗版限制）

- [x] 1.1 [RED] 補單元測試：① 帶 `X-Forwarded-For: 1.2.3.4, 5.6.7.8` 的 request 回傳 `1.2.3.4`（取第一個）；② 沒有 `X-Forwarded-For` 時退回 `REMOTE_ADDR`。確認這組測試現在 FAIL（函式不存在）— (auto) `pytest` 顯示 FAIL
- [x] 1.2 [GREEN] 實作：`apps/events/views.py` 新增 `_get_client_ip(request)`。讓 1.1 轉綠 — (auto) `pytest` 該函式測試全綠

## 2. Seam: Redis TTL 鎖（對應同一條 spec 需求）

- [x] 2.1 [RED] 在 `apps/events/tests/test_views.py` 補測試，涵蓋：① 同一 IP 對同一活動連續兩次留言（間隔 <2 秒）→ 第二次回傳 429、錯誤代碼 `COMMENT_RATE_LIMITED`，且第二次的留言內容沒有被寫入 DB；② 同一 IP 對**不同活動**連續留言 → 兩則都成功（鎖不跨活動）；③ 不同 IP 對同一活動連續留言 → 兩則都成功（鎖不跨 IP）；④ 等待鎖定時間過後（或直接操控 Redis TTL）→ 同一 IP 對同一活動可以再次留言成功；⑤ 留言驗證失敗（例如缺暱稱，400）→ 不消耗鎖，緊接著送出合法留言仍然成功；⑥ 模擬 Redis 連線失敗（例如指向不存在的位址）→ 留言仍正常寫入成功，不因為 Redis 異常而 500 或被擋下；⑦ 併發情境：同一 IP、同一活動，兩個內容皆合法的請求近乎同時送達（模擬連點）→ 只有一則成功寫入 DB，另一則收到 429，不能兩則都成功（驗證 D2/D3 的鎖必須設在寫入之前，不是之後）。確認這組測試現在 FAIL（目前沒有任何頻率限制）— (auto) `pytest` 顯示 FAIL
- [x] 2.2 [GREEN] 實作：`CommentListCreateView.post()` 流程改為：`serializer.is_valid(raise_exception=True)` 驗證通過後 → 組 Redis key（`comment_rl:{event_id}:{client_ip}`）→ 呼叫 `SET key 1 NX EX 2`（單一原子指令，見 design.md D3）→ 回傳失敗（key 已存在，代表鎖定中）則 `raise ApiError(..., code="COMMENT_RATE_LIMITED", status_code=429)`，**不呼叫** `serializer.save()`；回傳成功（搶到鎖）才繼續呼叫 `serializer.save()` 寫入 DB（見 design.md D2，上鎖在驗證之後、寫入之前，不是寫入之後）。Redis 連線例外（`redis.exceptions.RedisError` 等）一律捕捉、記 log、視同搶到鎖繼續執行 `serializer.save()`（D7 fail-open）。讓 2.1 全部轉綠 — (auto) `pytest apps/events/tests/test_views.py` 該檔全綠

## 3. 收尾

- [x] 3.1 全套驗證：`pytest -q`、`ruff check .`、`manage.py check`、`makemigrations --check --dry-run`（預期無變化）皆乾淨；`spectra validate add-comment-rate-limit --strict` 通過 — (auto)
- [x] 3.2 `/code-review`：跑一次自審，處理發現的真實問題（若有）— (auto)
- [ ] 3.3 跟 `jiu-sync-backend-cd-automated-deploy` repo 對齊：確認 nginx 設定檔有正確帶上 `X-Forwarded-For`（design.md D4 的外部依賴）— (manual，跨 repo)——跨 repo，不在此 repo 範圍
