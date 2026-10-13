# 揪甘心 jiu-sync-backend

揪團排時間服務的後端:**主揪**用 Google 帳號登入、建立活動與候選時段並分享連結;**參與者**免登入,以暱稱加手機末三碼投票、改票、留言;主揪可定案、取消或重新開放投票,定案後還能請 AI 推薦聚餐餐廳。

## 專案亮點

- **參與者免登入**:主揪走 Google SSO + JWT,參與者只靠暱稱與手機末三碼即可投票與留言。
- **完整活動生命週期**:投票 → 定案 / 取消 / 重新開放,狀態轉換由資料庫的條件式 UPDATE 把關,不只靠 Python 檢查。
- **AI 餐廳推薦**:定案後呼叫 Perplexity 推薦餐廳,以列鎖預留每人每月額度,避免付費 API 被超用。
- **附屬功能不拖垮主功能**:通知信在 transaction commit 後才交給 Celery 寄出,寄信、留言限流、可觀測性故障時 API 照常運作。
- **IaC 與可觀測性**:Terraform 建立 AWS EC2;Grafana Alloy 收集 metrics 與 log 送到 Grafana Cloud,送出前遮蔽 token 與 email。
- **規格驅動 + TDD**:每個功能都有 OpenSpec 的 proposal / design / tasks,CI 的 `spec-sync` 擋下沒有同步更新規格的實作變更。

## 技術棧

| 類別 | 使用 |
| --- | --- |
| 語言 / 套件管理 | Python 3.13、[uv](https://docs.astral.sh/uv/) |
| Web | Django 6.1、Django REST Framework、SimpleJWT、gunicorn |
| 資料 | PostgreSQL 16、PgBouncer(transaction pooling)、Redis 7 |
| 非同步 | Celery(broker 為 Redis) |
| 外部服務 | Google OAuth(id_token 驗證)、Perplexity(AI 推薦)、SMTP |
| 測試 / 品質 | pytest、pytest-django、factory-boy、ruff、k6(smoke test) |
| 部署 | Docker Compose、GHCR、AWS EC2 + SSM、Terraform、nginx |
| 可觀測性 | django-prometheus、Grafana Alloy、Grafana Cloud(Prometheus / Loki) |

## 系統架構

單台 AWS EC2(t2.micro)上以 docker compose 執行 `app`(Django + gunicorn)、`worker`(Celery)、`pgbouncer`、`db`(Postgres)、`redis` 與 `alloy`;nginx 裝在 host 上負責 TLS 終止與反向代理,是唯一對外的服務。app 本身不保存狀態(JWT 認證,資料都在 Postgres / Redis),寄信等非同步工作交給 Celery worker。

主要模組:`apps/accounts`(Google SSO、JWT)、`apps/events`(活動、投票、留言、生命週期)、`apps/recommendations`(AI 餐廳推薦與額度)、`apps/notifications`(通知信 Celery task)。

完整架構圖、請求流程與設計決策見 [docs/report/01-architecture.md](docs/report/01-architecture.md)。

## Quick Start

需求:Python 3.13、[uv](https://docs.astral.sh/uv/)、Docker。

```bash
cp .env.example .env    # 填入實際值
docker compose up -d    # 起 Postgres(5455)、PgBouncer(6433)、Redis(6381)
uv sync
uv run python manage.py migrate
uv run python manage.py runserver
uv run pytest           # 跑全套測試(需 Postgres 與 Redis)
```

環境變數範本見 [.env.example](.env.example),本機服務定義見 [docker-compose.yml](docker-compose.yml)。

Celery worker(`DJANGO_SETTINGS_MODULE` 必須明確指定,無預設值):

```bash
DJANGO_SETTINGS_MODULE=config.settings.dev uv run celery -A config worker -l info
```

## 部署與可觀測性

- **基礎設施**:[infra/terraform/](infra/terraform/) 在開發者本機執行,建立 EC2、Elastic IP、Security Group(入站僅 80/443,不開 22)與只帶 SSM 權限的 IAM Role。
- **CI**:[.github/workflows/ci.yml](.github/workflows/ci.yml) 在 PR 到 `main` / `develop` 時執行 `lint`、`test`、`test-pgbouncer`、`spec-sync`、`integration`(k6 smoke test)。
- **建置**:[.github/workflows/build-push.yml](.github/workflows/build-push.yml) 在 push 到 `develop` 時建置 image,推到 GHCR(`latest` 與 `sha-<7 碼>`)。
- **部署**:目前為手動觸發。開發者在本機執行 [infra/scripts/deploy.sh](infra/scripts/deploy.sh),透過 `aws ssm send-command` 在 EC2 上 pull image、`up -d`、`migrate`(直連 Postgres,繞過 PgBouncer)與 `collectstatic`。正式環境的 compose 檔為 [infra/docker/docker-compose.prod.yml](infra/docker/docker-compose.prod.yml)。
- **nginx**:裝在 EC2 host,負責 TLS(Let's Encrypt)、HTTP→HTTPS 與對外封鎖 `/metrics`;repo 內是參考副本,見 [infra/nginx/README.md](infra/nginx/README.md)。
- **可觀測性**:Grafana Alloy([infra/alloy/](infra/alloy/))以旁路方式收集 app metrics(`/metrics`,以 token 保護)、container 與主機指標、所有 container 與 nginx 的 log,送到 Grafana Cloud;dashboard JSON 在 [infra/grafana/dashboards/](infra/grafana/dashboards/)。

細節見 [docs/report/01-architecture.md](docs/report/01-architecture.md) 的 CI/CD、Terraform 與可觀測性章節。

## 開發流程

- **OpenSpec(規格驅動開發)**:每個功能先在 [openspec/changes/](openspec/changes/) 寫 proposal / design / tasks,再實作,完成後 archive;規格放在 [openspec/specs/](openspec/specs/)。
- **TDD**:先寫測試並確認紅燈,再寫實作轉綠,測試不可為了遷就程式碼而修改。
- 完整流程規則(工作流步驟、Task 拆分、資料操作穩健性、異常紀錄等)見 [CLAUDE.md](CLAUDE.md)。

## Branch 規範

- `main`:穩定版,只接受來自 `develop` 的 PR
- `develop`:整合分支(GitHub default branch),所有工作分支從這裡切出
- `feature/<name>`:新功能
- `fix/<name>`:修 bug
- `docs/<name>`:只改文件

以上工作分支一律從 `develop` 切出,完成後 PR 回 `develop`。

規則:**禁止直接 push 到 `main` / `develop`,一律走 PR + code review 後 merge。**

> Branch protection(強制 PR review 才能 merge)需要 GitHub Pro/Team 付費方案,目前 org 是 free plan、repo 是 private,GitHub 端規則暫時無法設定,此規範現階段靠團隊自律遵守。org 升級方案後補上 `main` / `develop` 的 required PR review 保護規則。

## 延伸文件

技術文件集中在 [docs/report/](docs/report/README.md)(資料基準日 2026-10-01):

| 文件 | 內容 |
| --- | --- |
| [technical-report.md](docs/report/technical-report.md) | 精簡技術報告(適合轉 PDF) |
| [01-architecture.md](docs/report/01-architecture.md) | 應用程式架構、CI/CD、Terraform、可觀測性 |
| [02-ux.md](docs/report/02-ux.md) | 介面 / 使用者體驗設計(後端角度) |
| [03-scalability.md](docs/report/03-scalability.md) | 擴展設計與效能 |
| [04-fault-tolerance.md](docs/report/04-fault-tolerance.md) | 容錯性設計 |
| [05-security.md](docs/report/05-security.md) | 安全性設計 |
| [06-tech-debt.md](docs/report/06-tech-debt.md) | 技術債處理 Log |
| [07-er-diagram.md](docs/report/07-er-diagram.md) | 資料模型與 ER 圖 |
| [08-uml-endpoints.md](docs/report/08-uml-endpoints.md) | UML 與 API 一覽 |
