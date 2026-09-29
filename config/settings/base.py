"""
Base Django settings for jiu-sync-backend (揪甘心).

Shared by settings.dev and settings.prod. Never import this module directly
as DJANGO_SETTINGS_MODULE.
"""

from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY", default="django-insecure-change-me-in-env")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])


# Application definition

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt",
    "corsheaders",
]

LOCAL_APPS = [
    "apps.accounts",
    "apps.events",
    "apps.recommendations",
    "apps.notifications",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


# Database
# PostgreSQL only — set DATABASE_URL, e.g.
# postgres://user:password@localhost:5455/jiu_sync
# (host port 5455, not Postgres's default 5432 — see docker-compose.yml)


def _database_config():
    """DATABASES["default"]: connection target from DATABASE_URL, plus options
    required behind PgBouncer transaction pooling (add-pgbouncer-and-celery-worker
    design.md D1). Server-side cursors don't survive across statements in
    transaction pooling; CONN_MAX_AGE reuses the client connection to PgBouncer
    (override with DB_CONN_MAX_AGE; 0 = close after each request), and health
    checks avoid reusing a connection PgBouncer already dropped."""
    config = env.db_url(
        "DATABASE_URL",
        default="postgres://jiu_sync:jiu_sync@localhost:5455/jiu_sync",
    )
    config["DISABLE_SERVER_SIDE_CURSORS"] = True
    config["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
    config["CONN_HEALTH_CHECKS"] = True
    return config


DATABASES = {
    "default": _database_config(),
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Custom User model — Google SSO only, no username/password auth. See
# apps.accounts.models.User and openspec/changes/google-sso-login/design.md.
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# Internationalization

LANGUAGE_CODE = "zh-hant"
TIME_ZONE = "Asia/Taipei"
USE_I18N = True
USE_TZ = True


# Static files

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"


# Email (Django 6.1 MAILERS API) — console backend by default; override SMTP
# in prod via env. Notifications per PRD §5 (finalized / deadline / board
# update, only to participants who filled in an email).

_SMTP_EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"


def _mailer_config(backend):
    """只有 SMTP backend 才帶 host/port/username/password/use_tls 這些
    OPTIONS——code-review 抓到:原本不管 BACKEND 是什麼都無條件塞這組 SMTP
    專屬參數，console backend（開發預設值）不吃這些參數，若 .env 剛好設了
    EMAIL_HOST 等變數但沒設 EMAIL_BACKEND（維持 console 預設），寄信當下會
    直接拋 InvalidMailer（design.md add-event-lifecycle Risks 2026-09-23
    修訂記錄，使用者本機實測過一次真的觸發，見 add-event-lifecycle
    design.md D10 2026-09-24 二輪修正）。
    """
    config = {"BACKEND": backend}
    if backend == _SMTP_EMAIL_BACKEND:
        config["OPTIONS"] = {
            "host": env("EMAIL_HOST", default=""),
            "port": env.int("EMAIL_PORT", default=587),
            "username": env("EMAIL_HOST_USER", default=""),
            "password": env("EMAIL_HOST_PASSWORD", default=""),
            "use_tls": env.bool("EMAIL_USE_TLS", default=True),
        }
    return config


MAILERS = {
    "default": _mailer_config(
        env(
            "EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend"
        )
    ),
}
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="jiu-sync@example.com")


# Django REST Framework

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    # Host-only endpoints require auth by default; participant/room endpoints
    # (PRD: fully login-free) must explicitly override to AllowAny per-view.
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    # 全站一致的非 2xx 錯誤回應格式 {"message": ..., "code": ...}. See
    # openspec/changes/api-error-format/design.md.
    "EXCEPTION_HANDLER": "config.exceptions.custom_exception_handler",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=14),
    "ROTATE_REFRESH_TOKENS": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}


# CORS — frontend SPA origin(s), comma-separated in env.

CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
# refresh token 走 httpOnly cookie（見
# openspec/changes/refresh-token-httponly-cookie/design.md），瀏覽器要願意收送這個
# cookie 需要 credentialed CORS。CORS_ALLOWED_ORIGINS 維持明確列出網域，不可為 "*"
# ——django-cors-headers 本身就禁止 "*" 搭配 CORS_ALLOW_CREDENTIALS=True。
CORS_ALLOW_CREDENTIALS = True

# Celery — background tasks: 7-day soft-delete expiry sweep, Email
# notifications (finalized / deadline / board update). Fire-and-forget: no
# result backend, nothing queries task results.

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6381/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
# 正式環境需要真的部署一個 Celery worker 程序消費佇列，預設 False（真非同步）。
# dev.py 覆寫成 True——本機/測試沒有另外跑 worker，.delay() 同步在原地執行完，
# 見 openspec/changes/add-event-lifecycle/design.md D2。
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)

# 留言防洗版鎖（`CommentListCreateView.post()`）專用的 Redis 連線，跟 Celery
# broker 用同一台 Redis instance、但獨立 DB index（db=1，Celery broker 用
# db=0）——避免鎖的 key 跟 Celery 佇列訊息混在同一個 keyspace，SCAN／監控／
# 未來清理都比較乾淨。見 openspec/changes/add-comment-rate-limit/design.md D5。
# 預設值直接把 CELERY_BROKER_URL 結尾的 db index 由 0 換成 1 算出來，兩者共用
# 同一台 Redis（同一個 docker-compose `redis` service），不需要額外開容器；
# CELERY_BROKER_URL 若不是以 "/0" 結尾（例如被覆寫成別的 db index），原樣沿用
# 不硬改，避免猜錯意圖。
COMMENT_RATE_LIMIT_REDIS_URL = env(
    "COMMENT_RATE_LIMIT_REDIS_URL",
    default=(
        CELERY_BROKER_URL[: -len("/0")] + "/1"
        if CELERY_BROKER_URL.endswith("/0")
        else CELERY_BROKER_URL
    ),
)


# Google SSO — host login only (see apps.accounts). Backend verifies the
# Google-issued id_token audience against this client ID.
GOOGLE_OAUTH_CLIENT_ID = env("GOOGLE_OAUTH_CLIENT_ID", default="")

# Event link lifetime per PRD §2.5 — soft-delete after N days, not hard delete.
EVENT_LINK_LIFETIME_DAYS = env.int("EVENT_LINK_LIFETIME_DAYS", default=7)

# Active AI recommendation backend. Pluggable strategy — see
# apps.recommendations.engines. "google_places_gemini" is the MVP default per
# PRD §4 (Plan A); "perplexity" is a documented, swappable alternative (Plan B)
# still under evaluation, not yet implemented.
RECOMMENDATION_ENGINE = env("RECOMMENDATION_ENGINE", default="google_places_gemini")
GOOGLE_PLACES_API_KEY = env("GOOGLE_PLACES_API_KEY", default="")
GEMINI_API_KEY = env("GEMINI_API_KEY", default="")
PERPLEXITY_API_KEY = env("PERPLEXITY_API_KEY", default="")

# Public origin of the frontend SPA — used to build shareable event links
# (e.g. f"{FRONTEND_BASE_URL}/events/{event.id}"). dev.py overrides the
# default; prod.py requires it to be set (fail-fast). See
# openspec/changes/add-events-api/design.md D5.
FRONTEND_BASE_URL = env("FRONTEND_BASE_URL", default=None)
