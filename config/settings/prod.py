from .base import *  # noqa: F403
from .base import ALLOWED_HOSTS, FRONTEND_BASE_URL, SECRET_KEY, env

DEBUG = False

if not ALLOWED_HOSTS:
    raise RuntimeError("DJANGO_ALLOWED_HOSTS must be set in production")

# 內部 scrape 放行(add-observability-stack design.md D3):Alloy 在 compose 內網以
# `http://app:8000/metrics` scrape(`Host: app`、不帶 X-Forwarded-Proto)。`app` 在程式碼
# 附加、不靠 .env;須在上面的空值檢查之後,否則會掩蓋 DJANGO_ALLOWED_HOSTS 未設定。
# 外部請求必經 nginx 並帶真實網域,`Host: app` 只會出現在 compose 內網。
_INTERNAL_SCRAPE_HOST = "app"
if _INTERNAL_SCRAPE_HOST not in ALLOWED_HOSTS:
    ALLOWED_HOSTS = [*ALLOWED_HOSTS, _INTERNAL_SCRAPE_HOST]

if not SECRET_KEY or SECRET_KEY.startswith("django-insecure-"):
    raise RuntimeError("DJANGO_SECRET_KEY must be set to a real secret in production")

if not FRONTEND_BASE_URL:
    raise RuntimeError("FRONTEND_BASE_URL must be set in production")

SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
# 只有 `/metrics`(無結尾斜線)不導向 https,供內部 scrape(D3);仍受 token 保護,
# 對外另由 nginx 回 404。SecurityMiddleware 以去掉開頭 `/` 的 path 做 re.search。
SECURE_REDIRECT_EXEMPT = [r"^metrics$"]
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=60 * 60 * 24 * 30)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
