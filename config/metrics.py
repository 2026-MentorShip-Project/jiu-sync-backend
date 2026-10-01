"""`/metrics`:Prometheus metrics 匯出,只給帶 bearer token 的收集端(Alloy)。

見 openspec/changes/add-observability-stack/design.md D2/D3:

- 包住 django-prometheus 的 `ExportToDjangoView`;它在設定 `PROMETHEUS_MULTIPROC_DIR`
  時改用 `MultiProcessCollector`,輸出所有 gunicorn worker 的加總。
- fail closed:`METRICS_TOKEN` 未設定 / 空字串(含只有空白),或 `Authorization` 不是
  完全等於 `Bearer <METRICS_TOKEN>` → `Http404`,走 `config.exceptions.handler404`,
  回應與不存在的路徑相同,不透露端點存在。
- 比對用 `hmac.compare_digest`(以 UTF-8 bytes 比較,非 ASCII 的 header 不會變 500)。
- `csrf_exempt`:否則未帶 token 的 POST 會先被 CsrfViewMiddleware 回 403,透露端點存在。
"""

import hmac

from django.conf import settings
from django.http import Http404
from django.views.decorators.csrf import csrf_exempt
from django_prometheus.exports import ExportToDjangoView


def _authorized(request):
    token = getattr(settings, "METRICS_TOKEN", "") or ""
    if not token.strip():
        return False
    provided = request.headers.get("Authorization", "")
    return hmac.compare_digest(provided.encode(), f"Bearer {token}".encode())


@csrf_exempt
def metrics(request):
    if not _authorized(request):
        raise Http404
    return ExportToDjangoView(request)
