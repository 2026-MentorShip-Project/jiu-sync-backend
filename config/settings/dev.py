from .base import *  # noqa: F403
from .base import CORS_ALLOWED_ORIGINS, FRONTEND_BASE_URL, env

DEBUG = True
ALLOWED_HOSTS = ["*"]
CORS_ALLOWED_ORIGINS = CORS_ALLOWED_ORIGINS or [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
FRONTEND_BASE_URL = FRONTEND_BASE_URL or "http://localhost:5173"

# 本機開發與測試都沒有另外跑 Celery worker，讓 .delay() 同步在原地執行完，
# 不需要真的接 Redis broker 也能跑通整條路徑；例外可用環境變數關掉，實際
# 對著 docker-compose 的 redis + 手動起 worker 測非同步行為。
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=True)
# 測試裡 task 本身拋例外要讓測試真的失敗，不能被吞掉。
CELERY_TASK_EAGER_PROPAGATES = env.bool("CELERY_TASK_EAGER_PROPAGATES", default=True)
