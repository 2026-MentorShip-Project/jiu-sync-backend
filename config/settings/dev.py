from .base import *  # noqa: F401,F403

DEBUG = True
ALLOWED_HOSTS = ["*"]
CORS_ALLOWED_ORIGINS = CORS_ALLOWED_ORIGINS or [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
