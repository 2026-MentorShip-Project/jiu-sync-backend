## Why

正式環境的 app 直接連 `db`(同機 `postgres:16` container),Django 未設 `CONN_MAX_AGE`,每個請求各開一條連線,沒有任何連線上限的保護;併發一高(或 AI 推薦等待上游時)連線數直接反映到 Postgres,在 t2.micro(1GB)上有把資料庫打爆的風險。

另外盤點時發現正式環境的背景任務完全沒有運作:`docker-compose.prod.yml` 沒有 celery worker,而且正式環境 `.env` 的 `CELERY_BROKER_URL` 是 `redis://localhost:6381/0`(container 內連不到 Redis)——建立/定案/取消/reopen 的通知信從未排入佇列(`_schedule_notification` 吞掉例外,API 仍成功),留言防洗版鎖連不到 Redis 而 fail-open,等於失效。兩者都是「正式環境 compose 的服務接線」,一起處理。

## What Changes

- 新增 PgBouncer(`edoburu/pgbouncer`,鎖定版本)於正式與本機 compose,transaction pooling、`default_pool_size=10`、`max_client_conn=100`;app(與 worker)改連 PgBouncer,不再直連 `db`
- Django:`DISABLE_SERVER_SIDE_CURSORS=True`、`CONN_MAX_AGE=60`、`CONN_HEALTH_CHECKS=True`
- `deploy.sh`:migrate 改以 `.env` 的 `DATABASE_URL_DIRECT` 直連 `db`;未設定即中止部署
- CI:新增經 PgBouncer 跑全套測試的 job
- 正式 compose 新增 celery `worker` service(同一 image、`--concurrency=1`、連 PgBouncer);正式環境 `CELERY_BROKER_URL` 修正為 `redis://redis:6379/0`
- 不改任何 API 行為、資料模型或通知 task 本身的重試行為

未涵蓋(明確排除):搬遷到 RDS / RDS Proxy;通知 task 的重試與冪等(會引入重複寄信問題,另開 change);Redis 持久化;SMTP 設定本身(本機與正式環境 `EMAIL_BACKEND` 皆已為 SMTP,worker 上線後即會實際寄信)。

## Capabilities

### New Capabilities

(無)

### Modified Capabilities

- `infra/app-deployment`:新增「資料庫連線經連線池」「背景任務在正式環境實際被執行」「資料庫 migration 直連資料庫」三項 requirement(路徑同 `deploy-django-app`)

## Impact

- `infra/docker/docker-compose.prod.yml`:新增 `pgbouncer`、`worker` service
- `docker-compose.yml`(本機):新增 `pgbouncer`(獨立 host port,預設 `DATABASE_URL` 仍直連)
- `config/settings/base.py`:`DATABASES` 連線選項
- `infra/scripts/deploy.sh`:migrate 直連、必要變數檢查
- `.github/workflows/ci.yml`:新增經 PgBouncer 的 test job
- EC2 `/opt/jiu-sync-backend/.env`(人工):`DATABASE_URL` 改指 `pgbouncer:6432`、新增 `DATABASE_URL_DIRECT`、修正 `CELERY_BROKER_URL`
- 新依賴:`edoburu/pgbouncer` image
