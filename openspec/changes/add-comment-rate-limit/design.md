## Context

留言板完全匿名、免登入，暱稱不驗證身分。`pyproject.toml` 已有 `redis` 套件依賴（裸 client，非 `django-redis`），目前唯一的 Redis 用途是 Celery broker（`CELERY_BROKER_URL`）。部署設計（`jiu-sync-backend-cd-automated-deploy` repo 的 `infra/docker/docker-compose.prod.yml`）明確寫「app 只透過 `127.0.0.1` 被 nginx 存取」——正式環境下 Django 收到的每個請求都經過 nginx。

## Decisions

### D1. Key 用「來源 IP + 活動 id」，不是暱稱

跟使用者確認：暱稱本身不驗證身分，換暱稱就能繞過鎖，防洗版效果打折；用 IP 才能真正擋住同一來源的連點腳本。「+ 活動 id」是為了不誤傷同時在留言不同活動的正常使用者，鎖的範圍限縮在單一活動內。

### D2. 上鎖時機：驗證通過之後、寫入 DB 之前上鎖（跟使用者確認後的實作細節修正）

跟使用者確認的原則是「驗證失敗（400）不該消耗鎖」——使用者單純打錯字被 400 擋掉，不該因此多等 2 秒才能修正重送，這不是洗版。

但實際上鎖的時間點需要落在 `serializer.is_valid()` 成功之後、`serializer.save()` 之前，而不是等 `save()` 也成功了才上鎖：這支端點要擋的正是「連點洗版」——也就是兩個內容都合法、幾乎同時送達的請求。如果鎖要等 DB 寫入完成才設定，兩個近乎同時抵達、都通過驗證的請求會在鎖生效前搶先都執行完 `save()`，變成兩則都寫入成功，完全達不到防洗版的目的（这正是最需要擋下的情境）。改成「驗證通過就立刻嘗試上鎖，搶到鎖才繼續寫入 DB，搶不到直接 429、不寫入」，同樣滿足「400 不消耗鎖」的原意（因為驗證失敗根本不會走到嘗試上鎖這一步），且真正達成防洗版效果。

### D3. 用 Redis `SET key 1 NX EX 2` 當上鎖／檢查合一的單一動作

`SET ... NX EX 2` 是單一 Redis 指令，天生原子——回傳成功即代表這次請求搶到鎖、可以繼續寫入 DB；回傳失敗（key 已存在）即代表目前在鎖定中，直接 429、不寫入。不用分成「先 `GET` 檢查」再「另外 `SET`」兩步（那樣兩個並發請求會都通過檢查階段、都寫入、都上鎖，一樣是 check-then-act 的 TOCTOU 問題），也不需要額外的分散式鎖或 `MULTI`/`EXEC`。

### D4. Client IP 來源：信任 `X-Forwarded-For`（取第一個值），退回 `REMOTE_ADDR`

正式環境下 `REMOTE_ADDR` 固定是 nginx 自己的位址，不是真正的使用者來源，必須讀 nginx 轉發的 `X-Forwarded-For` 才拿得到真實 client IP；本機開發沒有 nginx，退回 `REMOTE_ADDR`。

**外部依賴（本次 scope 外，需跨 repo 對齊）**：這個決策的前提是 nginx 設定檔有正確帶上 `X-Forwarded-For`——這是 `jiu-sync-backend-cd-automated-deploy` repo 的 nginx 設定範圍，不在這個 repo 可以驗證。實作前需要確認那邊的 nginx config 有沒有 `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`（或等效設定），沒有的話這個 helper 在正式環境會一律拿到 nginx 自己的位址，導致所有使用者共用同一把鎖。

### D5. 用獨立的 Redis DB index，不共用 Celery broker 的 DB 0

避免防洗版鎖的 key 跟 Celery 佇列訊息混在同一個 keyspace，SCAN/監控/未來清理都比較乾淨。新增 setting `COMMENT_RATE_LIMIT_REDIS_URL`，dev 預設把 `CELERY_BROKER_URL` 的 db index 從 0 換成 1（同一個 Redis instance，不需要額外開容器）。

### D6. 429 而不是其他狀態碼

HTTP 語意最貼近「頻率限制」的狀態碼，跟這個專案先前文件裡（已移除的 `responses/mine` TBD 草案）用的慣例一致。

### D7. Redis 連線失敗時 fail-open

跟使用者確認：防洗版是附加保護機制，不是核心功能，不應該因為基礎設施（Redis）問題拖垮核心的留言功能——跟現有通知信失敗不影響主動作是同一套哲學（`_schedule_notification` 既有模式）。連線異常（timeout／connection refused）時捕捉例外、記一筆 warning log、視同沒有鎖，讓留言正常寫入。

## Risks / Trade-offs

- **[風險] 共用 NAT／辦公室網路出口的多個正常使用者互相卡到對方的鎖**：使用者已確認接受這個取捨（D1），2 秒視窗很短，實際影響有限。
- **[風險] `X-Forwarded-For` 可被客戶端偽造**：這個 header 理論上任何人都能在請求裡自己帶——但因為 nginx 在最前面會覆寫/設定這個 header（D4 的前提），客戶端偽造的值不會是 nginx 實際看到的來源 IP，nginx 轉發時是用它自己觀察到的連線來源覆寫，不是原樣透傳使用者送的值（需要在 nginx 設定裡確認這點，屬於 D4 提到的外部依賴）。即使真的被繞過，後果只是「防洗版失效」，不是資料外洩或權限問題，風險可接受。
- **[風險] fail-open 代表 Redis 掛掉時防洗版完全失效**：這是 D7 的直接取捨，使用者已確認接受。

## Migration Plan

不需要新 migration。
