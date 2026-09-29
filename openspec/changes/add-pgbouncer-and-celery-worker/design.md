## Context

- 正式環境:單台 t2.micro(1GB),`docker-compose.prod.yml` 有 `app`(gunicorn gthread 3×4)、`db`(`postgres:16`)、`redis`(`redis:7`,無 volume);nginx 在 host。無 SSH,部署走 `infra/scripts/deploy.sh`(SSM)。
- Django 6.1 + psycopg 3;`DATABASES` 只由 `DATABASE_URL` 決定,未設 `CONN_MAX_AGE`。
- 正式 `.env` 的 `CELERY_BROKER_URL=redis://localhost:6381/0`(container 內不可達);正式 compose 無 worker。見 proposal Why。
- 動手前確認的不可妥協邊界(grill 前置):① 鎖與 `SET LOCAL` 必須在同一 transaction 內,由經 PgBouncer 的測試守住;② rollback = `.env` 的 `DATABASE_URL` 改回 `db:5432` 重新部署,PgBouncer 不持有狀態;③ migrate 直連;④ 連線池狀態可查;⑤ 跳過 explore。

## Goals / Non-Goals

**Goals:**
- 資料庫實際連線數有上限,AI 推薦等上游時不佔 Postgres 連線。
- 背景任務在正式環境真的被執行;留言防洗版恢復生效。
- 任一部分出問題可獨立回退。

**Non-Goals:**
- RDS / RDS Proxy;通知 task 重試與冪等;Redis 持久化;SMTP 設定。

## Decisions

### D1. PgBouncer transaction pooling(grill Q1)

`pool_mode=transaction`、`default_pool_size=10`、`max_client_conn=100`。Django autocommit 下沒有 transaction 的查詢一句一個 transaction,server 連線在查詢之間歸還;AI 推薦等上游 45 秒期間不佔 Postgres 連線。12 個 gunicorn thread + 1 worker 共用 10 條 server 連線,超出者在 PgBouncer 排隊。

Django 配合:`DISABLE_SERVER_SIDE_CURSORS=True`(transaction pooling 下 server-side cursor 跨語句失效)、`CONN_MAX_AGE=60` + `CONN_HEALTH_CHECKS=True`(重用到 PgBouncer 的 client 連線)。以 `.env` 可覆寫(`DB_CONN_MAX_AGE` 等)為準,預設值寫在 settings。

- 相容性前提:所有 `select_for_update`、`SET LOCAL` 都在 `transaction.atomic()` 內;不使用 advisory lock、`LISTEN/NOTIFY`、session 層級 `SET`。由 D4 的 CI job 守住。
- 替代:session pooling——一條 client 連線佔一條 server 連線,等上游時仍佔用,無實質 pooling,不採用。更小 pool(5)——尖峰排隊增加,不採用。

### D2. `edoburu/pgbouncer`,鎖定版本(grill Q2)

以環境變數設定(`DB_HOST=db`、`DB_USER`/`DB_PASSWORD` 取自既有 `POSTGRES_USER`/`POSTGRES_PASSWORD`、`AUTH_TYPE=scram-sha-256`、`POOL_MODE`、`DEFAULT_POOL_SIZE`、`MAX_CLIENT_CONN`、`LISTEN_PORT=6432`),不新增 secret。正式 compose 不綁任何 host port;本機 compose 綁 `6433` 供手動/測試使用,預設 `DATABASE_URL` 仍直連 `5455`。版本在環境健檢 task 實測後寫死,不用 `latest`。資料庫設定以萬用字元對應 `db`,使 pytest 建立的 `test_*` 資料庫也可經 PgBouncer 連線。

- 可觀測性:PgBouncer log 輸出到 container stdout;以 `psql -h pgbouncer -p 6432 -U <user> pgbouncer -c 'SHOW POOLS;'`(需設定 `ADMIN_USERS`/`STATS_USERS`)查看 `cl_waiting`、`sv_active`。指令寫進 tasks 的驗收步驟。
- 替代:自建 alpine image——需維護 ini/userlist/entrypoint 與 build 流程;host 安裝——設定不在 repo、無法測試。皆不採用。Bitnami image 已停止免費更新,不採用。

### D3. migrate 直連:`DATABASE_URL_DIRECT`(grill Q3)

`deploy.sh` 以既有 `extract_env_var` 讀 `DATABASE_URL_DIRECT`,執行 `docker compose exec -T -e DATABASE_URL="$DIRECT" app python manage.py migrate`;未設定 → 在 pull/up 之前即中止並輸出錯誤。`collectstatic` 不碰資料庫,維持原樣。Django 設定不新增 alias。

- 替代:第二個 `DATABASES` alias——可能被誤用於一般查詢;migrate 走 PgBouncer——非 atomic migration 在 transaction pooling 下可能失敗。皆不採用。

### D4. 驗證:本機 compose + CI 經 PgBouncer 的 test job(grill Q4)

`ci.yml` 新增 job `test-pgbouncer`:service container `postgres:16` + `edoburu/pgbouncer`(同版本、同 pool 設定),`DATABASE_URL` 指向 PgBouncer,跑全套 `pytest`。原 `test` job(直連)保留。PR #25(AI 推薦,含 `select_for_update`/`SET LOCAL` 併發測試)與本 change 後 merge 的一方,須在此 job 下全綠。

### D5. celery worker 最小可用(grill Q0/Q5)

正式 compose 新增 `worker`:同一 image,`command: celery -A config worker -l info --concurrency=1`,`env_file` 同 app(`DJANGO_SETTINGS_MODULE` 由 `.env`/image 提供,需明確為 `config.settings.prod`),`depends_on: [pgbouncer, redis]`,`restart: unless-stopped`。正式 `.env` 的 `CELERY_BROKER_URL` 改為 `redis://redis:6379/0`(防洗版鎖的 URL 由其推導為 `/1`,一併修正)。task 重試行為不變(best-effort,失敗不影響 API)。

- 替代:加 `autoretry_for`/`max_retries`——可能重複寄信,需冪等設計,另開 change;Redis 加 AOF volume——t2.micro 負擔、效益低。皆不採用。

## Risks / Trade-offs

- [transaction pooling 與 session 層級功能不相容] → D1 相容性前提 + D4 CI job 自動守住;新增程式若使用 session 層級功能,CI 會失敗。
- [PgBouncer 本身故障導致全站無法連 DB] → `restart: unless-stopped`;rollback:`.env` 的 `DATABASE_URL` 改回 `db:5432` 重新部署(PgBouncer 無狀態)。
- [t2.micro 記憶體] → PgBouncer 約數 MB;worker 約 100–150MB,`--concurrency=1`;部署後以 `free -m`/`docker stats` 觀察。
- [Redis 無持久化,重建時未處理的通知任務遺失] → 通知本就是 best-effort,接受;持久化另議。
- [worker 上線後開始實際寄信(本機與正式 `EMAIL_BACKEND` 皆為 SMTP)] → 之前排不進佇列的信不會補寄(當時 `.delay()` 已失敗);本機驗證(task 3.1)以環境變數覆寫為 console backend,避免寄出測試信;正式環境驗收(task 4.1)以實際收到信為準。
- [修正 broker URL 後防洗版鎖開始生效,行為改變] → 屬修復原設計,前端已有對應錯誤處理(沿用原 spec)。

## Migration Plan

1. merge 前:人工於 SSM session 修改 `.env`——`DATABASE_URL` 改為 `postgres://<user>:<pw>@pgbouncer:6432/<db>`、新增 `DATABASE_URL_DIRECT=postgres://<user>:<pw>@db:5432/<db>`、`CELERY_BROKER_URL=redis://redis:6379/0`(先備份 `.env`)。
2. merge → GHCR build → 本機執行 `deploy.sh`。
3. 驗證(見 tasks)。
4. Rollback:PgBouncer 問題 → `DATABASE_URL` 改回 `db:5432` 重新部署;worker 問題 → `docker compose stop worker`(通知回到未執行狀態,不影響 API)。
