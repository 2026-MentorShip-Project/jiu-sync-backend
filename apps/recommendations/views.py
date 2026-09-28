from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .engines import get_engine
from .quota import compute_quota


def serialize_quota(quota):
    """額度的 API 形狀。推薦成功回應的 ``quota`` 欄位也用這個(spec「查詢當月額度」)。"""
    return {
        "period": quota.period,
        "limit": quota.limit,
        "used": quota.used,
        "remaining": quota.remaining,
        "available": quota.available,
        "resetsAt": quota.resets_at.isoformat(),
    }


class AIRecommendationQuotaView(APIView):
    """``GET /api/me/ai-recommendation-quota/`` — 已登入使用者當月的 AI 推薦額度。

    掛在 config/urls.py,與 ``api/me/`` 並列(design.md D1)。外部服務未設定時仍回
    200,只把 ``serviceAvailable`` 設為 false。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        body = serialize_quota(compute_quota(request.user))
        body["serviceAvailable"] = get_engine().is_available()
        return Response(body)
