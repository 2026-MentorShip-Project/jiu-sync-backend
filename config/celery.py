import os

from celery import Celery

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    raise RuntimeError(
        "DJANGO_SETTINGS_MODULE must be set before starting Celery "
        "(e.g. config.settings.dev or config.settings.prod)."
    )

app = Celery("jiu_sync")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
