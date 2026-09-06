# jiu-sync-backend

揪甘心 — Django REST Framework 後端。套件管理用 uv，開發流程用 OpenSpec (SDD)。

## 骨架現況

- `config/settings/{base,dev,prod}.py` — 環境分離設定
- `apps/accounts` `apps/events` `apps/recommendations` `apps/notifications` — 空 app 骨架，待 openspec change 填入
- `config/celery.py` — Celery app（broker/backend 走 Redis）
- `docker-compose.yml` — 本地 Postgres + Redis
- `openspec/` — SDD 工作流程，指令規則詳見 `CLAUDE.md`

## 開發啟動

```bash
cp .env.example .env   # 填入實際值
docker compose up -d   # 起 Postgres + Redis
uv sync
uv run python manage.py migrate
uv run python manage.py runserver
```

Celery worker（`DJANGO_SETTINGS_MODULE` 必須明確指定，無預設值）：

```bash
DJANGO_SETTINGS_MODULE=config.settings.dev uv run celery -A config worker -l info
```

## 後續開發

功能規格與實作透過 OpenSpec 走：`openspec/changes/` 提案 → 審核 → apply → archive。
