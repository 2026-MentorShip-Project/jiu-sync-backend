from django.conf import settings
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from config.exceptions import ApiError, Gone

from .authentication import OptionalJWTAuthentication
from .lifecycle import compute_display_status
from .models import Event
from .serializers import (
    EventCreateSerializer,
    EventDetailSerializer,
    EventPatchSerializer,
    EventSummarySerializer,
)


def _get_event_or_404(id):
    """``GET``/``PATCH /api/events/{id}`` 共用的活動查找,查無資料時拋
    ``EVENT_NOT_FOUND``(404)。不用 ``get_object_or_404``——那樣拿到的是 DRF
    泛用的 ``NotFound``,只有粗粒度的 ``"NOT_FOUND"`` code。
    """
    event = Event.objects.select_related("owner", "final_slot").filter(pk=id).first()
    if event is None:
        raise ApiError(
            "找不到此活動，可能已被刪除或網址錯誤", code="EVENT_NOT_FOUND", status_code=404
        )
    return event


class EventListView(APIView):
    """``GET /api/events?owner=me`` — 已登入主揪查詢自己擁有的活動清單。

    只回傳 ``request.user`` 擁有的活動,不因 ``status`` 排除任何一筆(含已取消)。
    ``owner=me`` 目前是唯一支援的值、且為必填,缺少時拋
    ``serializers.ValidationError`` 讓既有 ``custom_exception_handler`` 統一包裝成 400。

    掛在跟 ``EventCreateView`` 相同的路徑(``/api/events/``)——Django URL
    resolver 依路徑字串比對、不依 HTTP method 分派,同一路徑只能對應一個 view
    class,所以讓 ``EventCreateView`` 繼承這個類別、疊加 ``post()`` 方法,兩者
    共用同一個 ``as_view()`` 掛載點。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        if request.query_params.get("owner") != "me":
            raise serializers.ValidationError(
                {"owner": "缺少必要查詢參數 owner=me"}, code="OWNER_PARAM_REQUIRED"
            )
        events = Event.objects.filter(owner=request.user).select_related(
            "owner", "final_slot"
        )
        serializer = EventSummarySerializer(
            events, many=True, context={"request": request}
        )
        return Response(serializer.data)


class EventCreateView(EventListView):
    """``POST /api/events`` — 已登入主揪建立活動與其候選時段。

    回應只含 ``{id, shareUrl}``,不回傳活動完整內容。``owner`` 一律取自
    ``request.user``,不採信請求內容中任何宣稱擁有者身分的欄位。繼承
    ``EventListView`` 只是為了共用同一個 URL 掛載點(見該類別 docstring)。
    """

    def post(self, request):
        serializer = EventCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # host_email 一律取自 request.user.email,不採信請求 body 中任何
        # hostEmail 欄位——EventCreateSerializer 根本不宣告該欄位。
        event = serializer.save(owner=request.user, host_email=request.user.email)

        # rstrip 避免 FRONTEND_BASE_URL 若帶結尾斜線組出雙斜線的 shareUrl。
        frontend_base_url = settings.FRONTEND_BASE_URL.rstrip("/")
        return Response(
            {
                "id": str(event.id),
                "shareUrl": f"{frontend_base_url}/events/{event.id}",
            },
            status=status.HTTP_201_CREATED,
        )


class EventDetailView(APIView):
    """``GET /api/events/{id}`` — 任何人(含未登入)可查詢活動完整資料。
    ``PATCH /api/events/{id}`` — 已登入擁有者編輯活動六個基本欄位。

    查無資料時拋 ``ApiError(..., code="EVENT_NOT_FOUND", status_code=404)``(不用
    ``get_object_or_404``,那樣拿到的是 DRF 泛用的 ``NotFound``,只有粗粒度的
    ``"NOT_FOUND"`` code,前端要的是活動專屬的 ``EVENT_NOT_FOUND``)。

    ``GET`` 額外處理連結失效:算出來的 ``displayStatus`` 是 ``"link_expired"``
    時(活動已定案/取消超過 7 天,見 ``lifecycle.compute_display_status``)回
    410 ``Gone``,不是 200——不用手動判斷 finalize/cancel 邏輯,直接複用既有的
    純函式。目前系統還沒有 finalize/cancel 端點,這個分支現在測不到真實觸發
    路徑,只能靠直接建立測試資料驗證(跟 ``compute_display_status`` 本身的其他
    分支一樣)。

    ``get_permissions()``/``get_authenticators()`` 依 ``self.request.method``
    分派——``GET`` 沿用 ``AllowAny`` + ``OptionalJWTAuthentication``(見該類別
    docstring)。``PATCH`` 改用 ``IsAuthenticated`` + 全域預設的嚴格
    ``JWTAuthentication``——壞 token 在 PATCH 語境下就該真的 401,不該像 GET
    一樣被吞成匿名。
    """

    def get_permissions(self):
        if self.request.method == "PATCH":
            return [IsAuthenticated()]
        return [AllowAny()]

    def get_authenticators(self):
        if self.request.method == "PATCH":
            return [JWTAuthentication()]
        return [OptionalJWTAuthentication()]

    def get(self, request, id):
        event = _get_event_or_404(id)
        display_status = compute_display_status(
            event.status,
            event.response_deadline,
            event.finalized_at,
            event.cancelled_at,
            event.final_slot.date if event.final_slot else None,
            timezone.now(),
        )
        if display_status == "link_expired":
            raise Gone("此活動連結已失效（活動結束超過7天）", code="LINK_EXPIRED")
        serializer = EventDetailSerializer(
            event, context={"request": request, "display_status": display_status}
        )
        return Response(serializer.data)

    def patch(self, request, id):
        event = _get_event_or_404(id)
        if request.user != event.owner:
            raise PermissionDenied("僅活動擁有者可編輯此活動")
        if event.status != Event.Status.ACTIVE:
            raise ApiError(
                "活動已定案或取消，無法編輯", code="EVENT_NOT_ACTIVE", status_code=409
            )

        serializer = EventPatchSerializer(event, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        response_serializer = EventDetailSerializer(event, context={"request": request})
        return Response(response_serializer.data)
