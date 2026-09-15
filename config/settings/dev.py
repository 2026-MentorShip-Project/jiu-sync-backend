from .base import *  # noqa: F403
from .base import CORS_ALLOWED_ORIGINS, FRONTEND_BASE_URL

DEBUG = True
ALLOWED_HOSTS = ["*"]
CORS_ALLOWED_ORIGINS = CORS_ALLOWED_ORIGINS or [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
FRONTEND_BASE_URL = FRONTEND_BASE_URL or "http://localhost:5173"
