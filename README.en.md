[繁體中文](README.md) | English

# 揪甘心 jiu-sync-backend

> This is an English translation of [README.md](README.md). If the two differ, the Chinese version is authoritative.

Backend for a group-scheduling service: the **host** signs in with a Google account, creates an event with candidate time slots, and shares a link; **participants** need no login and vote, change their votes, and comment using a nickname plus the last three digits of their phone number. The host can finalize, cancel, or reopen voting, and after finalizing can ask AI to recommend restaurants for the gathering.

## Highlights

- **No login for participants**: hosts use Google SSO + JWT; participants vote and comment with just a nickname and the last three digits of their phone number.
- **Full event lifecycle**: voting → finalize / cancel / reopen; state transitions are guarded by conditional UPDATEs in the database, not just Python checks.
- **AI restaurant recommendations**: after finalizing, Perplexity is called to recommend restaurants; a per-user monthly quota is reserved with a row lock so the paid API cannot be overused.
- **Auxiliary features never take down the core**: notification emails are handed to Celery only after the transaction commits; the API keeps working when email, comment rate limiting, or observability fails.
- **IaC and observability**: Terraform provisions AWS EC2; Grafana Alloy collects metrics and logs and ships them to Grafana Cloud, masking tokens and emails before sending.
- **Spec-driven + TDD**: every feature has an OpenSpec proposal / design / tasks; the CI `spec-sync` job blocks implementation changes that don't update the specs.

## Tech Stack

| Category | Used |
| --- | --- |
| Language / package management | Python 3.13, [uv](https://docs.astral.sh/uv/) |
| Web | Django 6.1, Django REST Framework, SimpleJWT, gunicorn |
| Data | PostgreSQL 16, PgBouncer (transaction pooling), Redis 7 |
| Async | Celery (Redis as broker) |
| External services | Google OAuth (id_token verification), Perplexity (AI recommendations), SMTP |
| Testing / quality | pytest, pytest-django, factory-boy, ruff, k6 (smoke test) |
| Deployment | Docker Compose, GHCR, AWS EC2 + SSM, Terraform, nginx |
| Observability | django-prometheus, Grafana Alloy, Grafana Cloud (Prometheus / Loki) |

## Architecture

A single AWS EC2 instance (t2.micro) runs `app` (Django + gunicorn), `worker` (Celery), `pgbouncer`, `db` (Postgres), `redis`, and `alloy` with docker compose; nginx is installed on the host for TLS termination and reverse proxying, and is the only publicly exposed service. The app itself is stateless (JWT authentication; all data lives in Postgres / Redis), and asynchronous work such as sending email is handed to the Celery worker.

Main modules: `apps/accounts` (Google SSO, JWT), `apps/events` (events, voting, comments, lifecycle), `apps/recommendations` (AI restaurant recommendations and quota), `apps/notifications` (notification email Celery tasks).

For the full architecture diagram, request flows, and design decisions, see [docs/report/01-architecture.md](docs/report/01-architecture.md).

## Quick Start

Requirements: Python 3.13, [uv](https://docs.astral.sh/uv/), Docker.

```bash
cp .env.example .env    # fill in real values
docker compose up -d    # starts Postgres (5455), PgBouncer (6433), Redis (6381)
uv sync
uv run python manage.py migrate
uv run python manage.py runserver
uv run pytest           # run the full test suite (requires Postgres and Redis)
```

See [.env.example](.env.example) for the environment variable template and [docker-compose.yml](docker-compose.yml) for local service definitions.

Celery worker (`DJANGO_SETTINGS_MODULE` must be set explicitly; there is no default):

```bash
DJANGO_SETTINGS_MODULE=config.settings.dev uv run celery -A config worker -l info
```

## Deployment & Observability

- **Infrastructure**: [infra/terraform/](infra/terraform/) runs on the developer's machine and creates the EC2 instance, an Elastic IP, a Security Group (inbound 80/443 only, port 22 closed), and an IAM Role with SSM permissions only.
- **CI**: [.github/workflows/ci.yml](.github/workflows/ci.yml) runs `lint`, `test`, `test-pgbouncer`, `spec-sync`, and `integration` (k6 smoke test) on PRs to `main` / `develop`.
- **Build**: [.github/workflows/build-push.yml](.github/workflows/build-push.yml) builds the image on push to `develop` and pushes it to GHCR (`latest` and `sha-<7 chars>`).
- **Deployment**: currently triggered manually. The developer runs [infra/scripts/deploy.sh](infra/scripts/deploy.sh) locally, which uses `aws ssm send-command` to pull the image, `up -d`, `migrate` (connecting directly to Postgres, bypassing PgBouncer), and `collectstatic` on EC2. The production compose file is [infra/docker/docker-compose.prod.yml](infra/docker/docker-compose.prod.yml).
- **nginx**: installed on the EC2 host; handles TLS (Let's Encrypt), HTTP→HTTPS redirects, and blocks external access to `/metrics`. The repo holds a reference copy; see [infra/nginx/README.md](infra/nginx/README.md).
- **Observability**: Grafana Alloy ([infra/alloy/](infra/alloy/)) collects, out-of-band as a sidecar, app metrics (`/metrics`, token-protected), container and host metrics, and logs from all containers and nginx, shipping them to Grafana Cloud; dashboard JSON lives in [infra/grafana/dashboards/](infra/grafana/dashboards/).

For details, see the CI/CD, Terraform, and observability sections of [docs/report/01-architecture.md](docs/report/01-architecture.md).

## Development Workflow

- **OpenSpec (spec-driven development)**: each feature starts with a proposal / design / tasks in [openspec/changes/](openspec/changes/), is then implemented, and is archived when done; specs live in [openspec/specs/](openspec/specs/).
- **TDD**: write tests first and confirm they fail (red), then implement until green; tests must not be changed to accommodate the code.
- For the full process rules (workflow steps, task splitting, data-operation robustness, incident log, etc.), see [CLAUDE.md](CLAUDE.md) (in Chinese).

## Branch Conventions

- `main`: stable; only accepts PRs from `develop`
- `develop`: integration branch (GitHub default branch); all work branches are cut from here
- `feature/<name>`: new features
- `fix/<name>`: bug fixes
- `docs/<name>`: documentation-only changes

All work branches are cut from `develop` and opened as PRs back into `develop` when done.

Rule: **never push directly to `main` / `develop`; always go through a PR + code review before merging.**

> Branch protection (requiring PR review before merging) needs a paid GitHub Pro/Team plan. The org is currently on the free plan and the repo is private, so the GitHub-side rules cannot be configured yet; for now the team follows this convention by self-discipline. Once the org upgrades its plan, required PR review protection will be added for `main` / `develop`.

## Further Reading

Technical documentation lives in [docs/report/](docs/report/README.md) (data as of 2026-10-01, written in Chinese):

| Document | Contents |
| --- | --- |
| [technical-report.md](docs/report/technical-report.md) | Condensed technical report (suitable for PDF export) |
| [01-architecture.md](docs/report/01-architecture.md) | Application architecture, CI/CD, Terraform, observability |
| [02-ux.md](docs/report/02-ux.md) | Interface / user experience design (backend perspective) |
| [03-scalability.md](docs/report/03-scalability.md) | Scalability design and performance |
| [04-fault-tolerance.md](docs/report/04-fault-tolerance.md) | Fault tolerance design |
| [05-security.md](docs/report/05-security.md) | Security design |
| [06-tech-debt.md](docs/report/06-tech-debt.md) | Tech debt log |
| [07-er-diagram.md](docs/report/07-er-diagram.md) | Data model and ER diagram |
| [08-uml-endpoints.md](docs/report/08-uml-endpoints.md) | UML and API overview |
