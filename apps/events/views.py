from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from .authentication import OptionalJWTAuthentication
from .models import Event
from .serializers import (
    EventCreateSerializer,
    EventDetailSerializer,
    EventPatchSerializer,
    EventSummarySerializer,
)


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
                {"owner": "缺少必要查詢參數 owner=me"}
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

    查無資料時 ``get_object_or_404`` 拋出的 ``Http404``,會被 DRF 預設的
    ``exception_handler`` 攔截轉成 ``NotFound``,再經
    ``config.exceptions.custom_exception_handler`` 統一包成 ``{message, code}``
    形狀,不需要額外接線。

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
        event = get_object_or_404(
            Event.objects.select_related("owner", "final_slot"), pk=id
        )
        serializer = EventDetailSerializer(event, context={"request": request})
        return Response(serializer.data)

    def patch(self, request, id):
        event = get_object_or_404(
            Event.objects.select_related("owner", "final_slot"), pk=id
        )
        if request.user != event.owner:
            raise PermissionDenied("僅活動擁有者可編輯此活動")
        if event.status != Event.Status.ACTIVE:
            raise serializers.ValidationError("僅進行中的活動可編輯")

        serializer = EventPatchSerializer(event, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        response_serializer = EventDetailSerializer(event, context={"request": request})
        return Response(response_serializer.data)
