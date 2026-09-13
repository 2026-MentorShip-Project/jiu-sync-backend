"""Platform-level health check.

See openspec/changes/add-ci-pipeline/design.md Decision 10/11:

- Lives in config/, not under any apps/* app — it's a "process is alive"
  signal for ops (CI health-check polling, k6 smoke test), not a product
  capability owned by any single app.
- Answers 200 without touching the DB. Whether the DB is reachable is
  already covered by the CI pipeline's `manage.py migrate` step; mixing
  that failure mode into this endpoint would make it harder to tell, from
  the log alone, which layer actually broke.
"""
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@api_view(["GET"])
@permission_classes([AllowAny])
def healthz(request):
    return Response({"status": "ok"})
