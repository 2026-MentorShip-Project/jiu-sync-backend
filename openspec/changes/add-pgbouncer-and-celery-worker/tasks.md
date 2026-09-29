> 會引入新依賴(`edoburu/pgbouncer` image),依 CLAUDE.md task 1 為環境健檢。各 task 可獨立驗收、獨立回退。

## 1. 環境健檢

- [ ] 1.1 確認 `docker` 可用、`docker pull edoburu/pgbouncer:<版本>` 成功並記錄選定的明確版本 tag(查 image 的 tag 列表,選最新穩定版,記下 PgBouncer 版本號);確認該版本支援 `AUTH_TYPE=scram-sha-256` 與萬用字元資料庫設定;`git status`/`git log` 正常且位於 `feature/pgbouncer`;本機 `docker-compose.yml` 的 `db`/`redis` 可啟動 — (auto)

## 2. PgBouncer 連線池(D1–D4)

- [ ] 2.1 [RED→GREEN] ① 新增測試:`DATABASES["default"]` 的 `DISABLE_SERVER_SIDE_CURSORS` 為 True、`CONN_MAX_AGE` 預設 60 且可由環境變數覆寫、`CONN_HEALTH_CHECKS` 為 True;先確認 FAIL 再改 `config/settings/base.py`。② 本機 `docker-compose.yml` 新增 `pgbouncer`(1.1 的版本、transaction pooling、pool 10/100、scram、萬用字元資料庫、綁 host `6433`),預設 `DATABASE_URL` 不變。③ 正式 `docker-compose.prod.yml` 新增 `pgbouncer`(不綁 host port、`restart: unless-stopped`、`depends_on: db`),`app` 改 `depends_on: pgbouncer`,檔頭註解更新 `.env` 需要的 `DATABASE_URL`(指 `pgbouncer:6432`)與 `DATABASE_URL_DIRECT`。④ `deploy.sh`:讀 `DATABASE_URL_DIRECT`,未設定則在 pull 前中止並輸出明確錯誤;migrate 以 `-e DATABASE_URL=...` 直連;以 `bash -n` 與本機模擬(抽出檢查函式或以 dry-run 方式)驗證缺值時中止。⑤ `ci.yml` 新增 `test-pgbouncer` job(postgres + pgbouncer service、`DATABASE_URL` 指向 pgbouncer)。驗證:本機以 `DATABASE_URL=postgres://jiu_sync:jiu_sync@localhost:6433/jiu_sync` 跑全套 `pytest -q` 全綠、直連同樣全綠;`docker compose -f infra/docker/docker-compose.prod.yml config` 通過;`SHOW POOLS` 可查到連線池狀態並記錄指令 — (auto)

## 3. celery worker 與 broker URL 修正(D5)

- [ ] 3.1 正式 `docker-compose.prod.yml` 新增 `worker` service(同 image、`celery -A config worker -l info --concurrency=1`、`DJANGO_SETTINGS_MODULE=config.settings.prod`、`depends_on: [pgbouncer, redis]`、`restart: unless-stopped`);檔頭註解改為明確要求 `CELERY_BROKER_URL=redis://redis:6379/0`。驗證:`docker compose -f infra/docker/docker-compose.prod.yml config` 通過;本機以與正式相同的服務組合(app + worker + pgbouncer + db + redis,`CELERY_TASK_ALWAYS_EAGER=false`、以環境變數覆寫 `EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend`,不得寄出真實信件)起一次,建立活動後 worker log 顯示通知 task 成功執行、2 秒內連發兩則留言第二則被拒 — (auto)

## 4. 正式環境切換

- [ ] 4.1 人工:SSM session 備份並修改 `/opt/jiu-sync-backend/.env`(`DATABASE_URL` 指 `pgbouncer:6432`、新增 `DATABASE_URL_DIRECT` 指 `db:5432`、`CELERY_BROKER_URL=redis://redis:6379/0`;`EMAIL_BACKEND` 已確認為 SMTP);merge 後 GHCR build 完成,本機執行 `deploy.sh`。驗證:`curl https://56-155-65-244.sslip.io/api/me/` 回本專案 401 格式;`SHOW POOLS` 有 `sv_active`/`cl_active` 紀錄;`pg_stat_activity` 的連線來源為 pgbouncer container;建立一個測試活動(主揪 email 為自己)後 worker log 顯示通知 task 完成且實際收到通知信;`free -m` 記錄記憶體餘量。Rollback 步驟見 design.md Migration Plan — (manual)
