> 會引入新依賴(`edoburu/pgbouncer` image),依 CLAUDE.md task 1 為環境健檢。各 task 可獨立驗收、獨立回退。

## 1. 環境健檢

- [x] 1.1 確認 `docker` 可用、`docker pull edoburu/pgbouncer:<版本>` 成功並記錄選定的明確版本 tag(查 image 的 tag 列表,選最新穩定版,記下 PgBouncer 版本號);確認該版本支援 `AUTH_TYPE=scram-sha-256` 與萬用字元資料庫設定;`git status`/`git log` 正常且位於 `feature/pgbouncer`;本機 `docker-compose.yml` 的 `db`/`redis` 可啟動 — (auto)
  - 結果(2026-09-29):
    - 環境:Docker 29.2.1、Docker Compose v5.1.0;`git` 位於 `feature/pgbouncer`、toplevel 為本 repo、working tree clean;`docker compose up -d db redis` 正常(postgres:16,`password_encryption=scram-sha-256`,pg_hba 對外 `scram-sha-256`)。
    - 選定 image:`edoburu/pgbouncer:v1.25.2-p0`(Docker Hub 最新明確 tag,2026-06-10),index digest `sha256:7d7a27d9e90985cab5cf42256f5c13a3120baa4b055b69df37beb272b89b2340`,內含 **PgBouncer 1.25.2**;multi-arch(linux/amd64、linux/arm64)。image 預設 `EXPOSE 5432`、以 `postgres` 使用者執行。
    - entrypoint(`/entrypoint.sh`)行為:未設 `DB_NAME`/`DATABASE_URL` 時產生 `* = host=${DB_HOST} port=${DB_PORT:-5432} auth_user=${DB_USER}`(萬用字元);`AUTH_TYPE=scram-sha-256` 時 userlist 直接寫**明文密碼**(`"jiu_sync" "jiu_sync"`),PgBouncer 以明文密碼對 server 做 SCRAM,不需自行產生 verifier;`LISTEN_PORT` 預設 **5432**(必須明確設 6432);`ADMIN_USERS` 預設 `postgres`(必須明確設);`STATS_USERS`、`POOL_MODE`、`DEFAULT_POOL_SIZE`、`MAX_CLIENT_CONN` 皆支援;`/etc/pgbouncer/pgbouncer.ini` 已存在時不重新產生。
    - 實測可用的環境變數組合(本機 compose 網路 `jiu-sync-backend_default`,`-p 6433:6432`):
      `DB_HOST=db` `DB_USER=jiu_sync` `DB_PASSWORD=jiu_sync` `AUTH_TYPE=scram-sha-256` `POOL_MODE=transaction` `DEFAULT_POOL_SIZE=10` `MAX_CLIENT_CONN=100` `LISTEN_PORT=6432` `ADMIN_USERS=jiu_sync` `STATS_USERS=jiu_sync`(不設 `DB_NAME`)。
    - 驗證:經 `localhost:6433` 連 `jiu_sync` 執行 `select 1` 成功;連未列於設定的資料庫(臨時建立的 `pgb_check_tmp`)經萬用字元成功;經 `postgres` 資料庫 `CREATE DATABASE` 成功;錯誤密碼回 `SASL authentication failed`;不存在的資料庫回 `database "..." does not exist`。
    - `SHOW POOLS` 指令(可執行,已看到 `cl_active`/`cl_waiting`/`sv_active`/`sv_idle`/`pool_mode=transaction`):
      本機 `PGPASSWORD=jiu_sync psql "host=localhost port=6433 user=jiu_sync dbname=pgbouncer" -c 'SHOW POOLS;'`;容器網路內 `psql -h pgbouncer -p 6432 -U <user> pgbouncer -c 'SHOW POOLS;'`。
    - **與 design 未涵蓋的落差(待 `opsx:update` 決策,不在本 task 處理)**:PgBouncer 會保留對 `test_jiu_sync` 的 idle server 連線,pytest-django 在 teardown 經 PgBouncer `DROP DATABASE` 失敗(`database "test_jiu_sync" is being accessed by other users`)。實測 `DATABASE_URL=...@localhost:6433/jiu_sync pytest -q apps/notifications`:第一次 13 passed + teardown PytestWarning;不重啟 PgBouncer 再跑第二次,因殘留的 `test_jiu_sync` 無法刪除,13 errors。影響 task 2.1 的「本機經 PgBouncer 跑全套 pytest」與 D4 的 CI job(CI 每次為全新 container,預期僅 teardown warning,但未實測)。
    - 臨時容器 `pgb-check` 已停止(`--rm`),臨時資料庫已刪除,既有 compose volume 未動。

## 2. PgBouncer 連線池(D1–D4)

- [x] 2.1 [RED→GREEN] ① 新增測試:`DATABASES["default"]` 的 `DISABLE_SERVER_SIDE_CURSORS` 為 True、`CONN_MAX_AGE` 預設 60 且可由環境變數覆寫、`CONN_HEALTH_CHECKS` 為 True;先確認 FAIL 再改 `config/settings/base.py`。② 本機 `docker-compose.yml` 新增 `pgbouncer`(1.1 的版本、transaction pooling、pool 10/100、scram、萬用字元資料庫、綁 host `6433`),預設 `DATABASE_URL` 不變。③ 正式 `docker-compose.prod.yml` 新增 `pgbouncer`(不綁 host port、`restart: unless-stopped`、`depends_on: db`),`app` 改 `depends_on: pgbouncer`,檔頭註解更新 `.env` 需要的 `DATABASE_URL`(指 `pgbouncer:6432`)與 `DATABASE_URL_DIRECT`。④ `deploy.sh`:讀 `DATABASE_URL_DIRECT`,未設定則在 pull 前中止並輸出明確錯誤;migrate 以 `-e DATABASE_URL=...` 直連;以 `bash -n` 與本機模擬(抽出檢查函式或以 dry-run 方式)驗證缺值時中止。⑤ [RED→GREEN] `conftest.py` 經 PgBouncer 的測試資料庫清理(design.md D4 Q6):先寫測試——未設 `PGBOUNCER_ADMIN_URL` 時不連 PgBouncer、不做任何事;有設定時於收尾對管理資料庫下 `KILL <測試資料庫名>`;KILL 失敗時拋出例外不吞掉——確認 FAIL 後實作。⑥ `ci.yml` 新增 `test-pgbouncer` job(postgres + pgbouncer service、`DATABASE_URL` 指向 pgbouncer、設定 `PGBOUNCER_ADMIN_URL`)。驗證:本機以 `DATABASE_URL=postgres://jiu_sync:jiu_sync@localhost:6433/jiu_sync` 並設定 `PGBOUNCER_ADMIN_URL`,**不重啟 PgBouncer 連續跑兩次**全套 `pytest -q` 皆全綠且無 DROP 失敗 warning;直連(不設 `PGBOUNCER_ADMIN_URL`)同樣全綠;`docker compose -f infra/docker/docker-compose.prod.yml config` 通過;`SHOW POOLS` 可查到連線池狀態並記錄指令 — (auto)
  - 結果(2026-09-29):
    - RED:① `config/tests/test_database_config.py`(7 則,含 `DB_CONN_MAX_AGE=0`、非數字 → `ValueError`)先因 `_database_config` 不存在而收集失敗;⑤ `config/tests/test_pgbouncer_cleanup.py` 先因 `config.pgbouncer_cleanup` 不存在而收集失敗,code-review 後追加的逾時上限/log 兩則先 2 failed。實作後全綠。
    - ① `config/settings/base.py` 新增 `_database_config()`:`DISABLE_SERVER_SIDE_CURSORS=True`、`CONN_MAX_AGE=env.int("DB_CONN_MAX_AGE", 60)`、`CONN_HEALTH_CHECKS=True`。
    - ⑤ `config/pgbouncer_cleanup.py` + 根目錄 `conftest.py`(包一層 session 範圍 `django_db_setup`,收尾先關 Django 連線再清理)。**與 design.md D4 的落差**:實測 PgBouncer `KILL <db>` 後該資料庫維持暫停、新連線卡到逾時,因此實作為 `KILL` 後立即 `RESUME`(任一失敗拋例外,管理連線 `connect_timeout=5`、KILL/RESUME 皆寫 log);design.md D4 措辭待 `opsx:update` 補上 `RESUME`。
    - 驗證:本機經 PgBouncer(`DATABASE_URL=...@localhost:6433/jiu_sync`、`PGBOUNCER_ADMIN_URL=...@localhost:6433/pgbouncer`)不重啟 PgBouncer 連跑兩次全套 314 passed、無 teardown warning、跑完無殘留 `test_*` 資料庫;對照組(經 PgBouncer 但不設 `PGBOUNCER_ADMIN_URL`)重現 DROP 失敗 warning;直連 314 passed。`ruff check .`、`manage.py check`(dev/prod)乾淨。
    - ③ `docker compose --env-file <假 .env> -f infra/docker/docker-compose.prod.yml config` 通過,`pgbouncer` 無 `ports`、`app.depends_on=[pgbouncer, redis]`。
    - ④ `bash -n` 通過;將 deploy.sh 的 remote script heredoc 在本機展開、以假 `.env` 與 stub `docker` 執行(未執行 deploy.sh 本體、未呼叫 AWS):缺 `DATABASE_URL_DIRECT` 或空值 → exit 1、docker 呼叫 0 次;有值(含引號與 `$`)→ migrate 為 `exec -T -e DATABASE_URL=<直連值> app python manage.py migrate`,字面值保留。
    - ⑥ `ci.yml` 新增 `test-pgbouncer`(`*postgres-service`、新增 `&redis-service` anchor、pgbouncer service 同本機參數、`DB_HOST=postgres`、健康檢查 `pg_isready`(已確認 image 內有)、收尾 `SHOW POOLS`);YAML 解析通過。**CI 實際執行需 push 後確認。**
    - `SHOW POOLS`:本機 `PGPASSWORD=jiu_sync psql "host=localhost port=6433 user=jiu_sync dbname=pgbouncer" -c 'SHOW POOLS;'`;容器內 `docker compose exec pgbouncer psql -h 127.0.0.1 -p 6432 -U <user> pgbouncer -c 'SHOW POOLS;'`(實測可查,`pool_mode=transaction`)。
    - `spectra analyze` 0.03s(僅已知誤報);`spectra drift` 0.27s,MEDIUM:`--concurrency`、`--create-db`、`--reuse-db` 被判為 broken anchor(後兩者為 pytest-django 旗標,`pytest --help` 有列,誤報)。

## 3. celery worker 與 broker URL 修正(D5)

- [ ] 3.1 正式 `docker-compose.prod.yml` 新增 `worker` service(同 image、`celery -A config worker -l info --concurrency=1`、`DJANGO_SETTINGS_MODULE=config.settings.prod`、`depends_on: [pgbouncer, redis]`、`restart: unless-stopped`);檔頭註解改為明確要求 `CELERY_BROKER_URL=redis://redis:6379/0`。驗證:`docker compose -f infra/docker/docker-compose.prod.yml config` 通過;本機以與正式相同的服務組合(app + worker + pgbouncer + db + redis,`CELERY_TASK_ALWAYS_EAGER=false`、以環境變數覆寫 `EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend`,不得寄出真實信件)起一次,建立活動後 worker log 顯示通知 task 成功執行、2 秒內連發兩則留言第二則被拒 — (auto)

## 4. 正式環境切換

- [ ] 4.1 人工:SSM session 備份並修改 `/opt/jiu-sync-backend/.env`(`DATABASE_URL` 指 `pgbouncer:6432`、新增 `DATABASE_URL_DIRECT` 指 `db:5432`、`CELERY_BROKER_URL=redis://redis:6379/0`;`EMAIL_BACKEND` 已確認為 SMTP);merge 後 GHCR build 完成,本機執行 `deploy.sh`。驗證:`curl https://56-155-65-244.sslip.io/api/me/` 回本專案 401 格式;`SHOW POOLS` 有 `sv_active`/`cl_active` 紀錄;`pg_stat_activity` 的連線來源為 pgbouncer container;建立一個測試活動(主揪 email 為自己)後 worker log 顯示通知 task 完成且實際收到通知信;`free -m` 記錄記憶體餘量。Rollback 步驟見 design.md Migration Plan — (manual)
