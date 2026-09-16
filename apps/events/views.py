from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import OptionalJWTAuthentication
from .models import Event
from .serializers import EventCreateSerializer, EventDetailSerializer, EventSummarySerializer


class EventListView(APIView):
    """``GET /api/events?owner=me`` — 已登入主揪查詢自己擁有的活動清單。

    只回傳 ``request.user`` 擁有的活動,不因 ``status`` 排除任何一筆(含已取消)
    ——見 specs/events/spec.md「主揪查詢自己擁有的活動清單」。``owner=me`` 查詢
    參數目前是唯一支援的值、且為必填:缺少時視為缺少必要參數,拋
    ``serializers.ValidationError`` 讓既有 ``custom_exception_handler`` 統一包裝成
    400——沿用 design.md D8「驗證失敗沿用既有錯誤處理」的慣例,不新增例外類型。

    這支 view 掛在跟 ``EventCreateView`` 相同的路徑(``/api/events/``)。Django
    URL resolver 依路徑字串比對、不依 HTTP method 分派,同一路徑只能對應一個
    view class,所以讓 ``EventCreateView`` 繼承這個類別、疊加 ``post()`` 方法,
    兩者共用同一個 ``as_view()`` 掛載點——概念上等同 DRF
    ``generics.ListCreateAPIView`` 把 list/create 兩個 mixin 合成一個 view 的慣例,
    只是這裡手動拆成兩個可各自獨立測試/閱讀的類別,不是每個 verb 各開一條路由
    (那樣兩個 ``path("", ...)`` 會讓第二個永遠比對不到、且合併成一個 view 才能
    保留 DRF ``APIView.as_view()`` 內建的 csrf_exempt 包裝,不必額外處理 CSRF)。
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

    回應只含 ``{id, shareUrl}``,不回傳活動完整內容(不用序列化器的完整
    representation)——見 openspec/changes/add-events-api/design.md D5、
    specs/events/spec.md「主揪建立活動」。``owner`` 一律取自
    ``request.user``,不採信請求內容中任何宣稱擁有者身分的欄位。

    繼承 ``EventListView`` 只是為了共用同一個 URL 掛載點(見該類別 docstring),
    ``permission_classes`` 剛好兩者皆為 ``IsAuthenticated`` 所以不需覆寫。
    """

    def post(self, request):
        serializer = EventCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # host_email 一律取自 request.user.email(帳號建立時就必填、唯一的欄位),
        # 不採信請求 body 中任何 hostEmail 欄位——EventCreateSerializer 根本不宣告
        # 該欄位,即使帶了也不會出現在 validated_data 裡。見 spec「主揪建立活動」與
        # design.md D6(2026-09-16 修正:改為建立當下即自動代入,不再等待未來的
        # PATCH 端點)。
        event = serializer.save(owner=request.user, host_email=request.user.email)

        # rstrip 避免 FRONTEND_BASE_URL 若被設成帶結尾斜線(例如
        # "https://example.com/")時組出雙斜線的 shareUrl。見 Codex review 修正
        # 項目 D。
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

    查無資料時 ``get_object_or_404`` 拋出的 ``django.http.Http404``,會被 DRF
    預設的 ``exception_handler`` 攔截轉成 ``NotFound``,再經
    ``config.exceptions.custom_exception_handler`` 統一包成 ``{message, code}``
    形狀——不需要額外接線,也不是走 Django URL resolver 層級的 ``handler404``
    (那個只在路由本身比對不到時觸發,這裡路由是比對得到的)。見
    openspec/changes/add-events-api/design.md D8。

    ``authentication_classes`` 覆寫成 ``OptionalJWTAuthentication``(見該類別
    docstring)——``AllowAny`` 只跳過權限檢查,壞掉/過期的 Bearer token 若用全域
    嚴格版 ``JWTAuthentication``,仍會在認證階段就讓整支 request 401,跟這支端點
    「任何人皆可查詢」的公開性矛盾。見 Codex review 修正項目 A。
    """

    permission_classes = [AllowAny]
    authentication_classes = [OptionalJWTAuthentication]

    def get(self, request, id):
        event = get_object_or_404(
            Event.objects.select_related("owner", "final_slot"), pk=id
        )
        serializer = EventDetailSerializer(event, context={"request": request})
        return Response(serializer.data)
