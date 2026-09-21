import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
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
from .models import Event, ParticipantResponseAccessToken, ParticipantResponseSlotAvailability
from .serializers import (
    EventCreateSerializer,
    EventDetailSerializer,
    EventPatchSerializer,
    EventSummarySerializer,
    ParticipantResponseCreateSerializer,
    ParticipantResponsePatchSerializer,
    ParticipantResponseVerifySerializer,
)

# ParticipantResponseAccessToken 的效期,核發後固定 30 分鐘(design.md D2)。
PARTICIPANT_ACCESS_TOKEN_TTL = timedelta(minutes=30)

# 暱稱查無資料時,仍對這個固定雜湊值跑一次 check_password,讓「暱稱不存在」與
# 「暱稱存在但手機碼錯誤」兩種失敗耗費的時間趨於一致——design.md D3 只保證
# 回應「內容」不洩漏差異,若略過雜湊比對直接短路,兩種失敗的回應時間仍可被用來
# 側錄暱稱是否存在(code-review 抓到)。模組載入時算一次即可,不用每次請求
# 重新雜湊。
_DUMMY_PHONE_HASH_FOR_TIMING = make_password("000")


def _get_event_or_404(id, queryset=None):
    """``GET``/``PATCH /api/events/{id}`` 與參與者三支端點共用的活動查找,查無
    資料時拋 ``EVENT_NOT_FOUND``(404)。不用 ``get_object_or_404``——那樣拿到的
    是 DRF 泛用的 ``NotFound``,只有粗粒度的 ``"NOT_FOUND"`` code。

    ``queryset`` 預設 ``None`` 時用最小的 ``select_related``——前置條件檢查、
    token 驗證這類不需要序列化整筆活動的呼叫點不用背負額外的 prefetch。
    ``EventDetailView`` 的 ``GET``/``PATCH``,以及 ``ParticipantResponseCreateView``/
    ``ParticipantResponseDetailView``(D16,寫入後改回傳完整活動內容)都會回傳
    ``EventDetailSerializer`` 結果,皆傳入 ``_event_with_responses_queryset()``
    換掉預設值,避免 N+1;不影響其他呼叫端。
    """
    if queryset is None:
        queryset = Event.objects.select_related("owner", "final_slot")
    event = queryset.filter(pk=id).first()
    if event is None:
        raise ApiError(
            "找不到此活動，可能已被刪除或網址錯誤", code="EVENT_NOT_FOUND", status_code=404
        )
    return event


def _event_with_responses_queryset():
    """所有會回傳完整 ``EventDetailSerializer`` 結果的 view 共用
    (``EventDetailView`` 的 ``GET``/``PATCH``,以及三態投票寫入後的
    ``ParticipantResponseCreateView``/``ParticipantResponseDetailView``,見
    D16)。``prefetch_related("slots", "responses__slot_availabilities")``
    避免 ``get_responses()``/``get_slotSummary()``(D17)各自造成 N+1,抽成
    共用函式避免多處重複一次一模一樣的 queryset 組合。
    """
    return Event.objects.select_related("owner", "final_slot").prefetch_related(
        "slots", "responses__slot_availabilities"
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
        event = _get_event_or_404(id, queryset=_event_with_responses_queryset())
        display_status = _display_status_or_410(event)
        serializer = EventDetailSerializer(
            event, context={"request": request, "display_status": display_status}
        )
        return Response(serializer.data)

    def patch(self, request, id):
        event = _get_event_or_404(id, queryset=_event_with_responses_queryset())
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


def _display_status_or_410(event):
    """算出 event 的 displayStatus;連結已失效(``"link_expired"``)時直接拋
    410 ``Gone``,否則回傳算出的 displayStatus 字串。``EventDetailView.get()``
    與 ``_check_participation_preconditions`` 共用同一份判斷邏輯,避免兩處各自
    重複一次 ``compute_display_status(...)`` 呼叫與 link_expired 判斷。
    """
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
    return display_status


def _check_participation_preconditions(event):
    """三支參與者端點(建立投票／身分核對／更新投票)共用前置條件檢查:
    連結未失效 → 活動狀態為進行中 → 未過投票截止時間。見
    openspec/changes/add-participant-responses/design.md D6。

    ``VOTING_CLOSED`` 統一回 409(狀態衝突,跟 EVENT_NOT_ACTIVE 同一類、也跟既有
    PATCH /api/events/{id} 慣例一致)。曾在 D10 短暫讓
    ``ParticipantResponseCreateView`` 單獨改回 400,使用者事後確認要統一改回
    409,見 D10 修訂記錄。
    """
    _display_status_or_410(event)
    if event.status != Event.Status.ACTIVE:
        raise ApiError(
            "活動已取消或已定案，無法投票", code="EVENT_NOT_ACTIVE", status_code=409
        )
    if timezone.now() >= event.response_deadline:
        raise ApiError(
            "投票已截止，請聯繫主揪重新開放投票", code="VOTING_CLOSED", status_code=409
        )


class ParticipantResponseCreateView(APIView):
    """``POST /api/events/{id}/responses`` — 任何人(含未登入)透過活動分享連結
    提交初次投票。完全公開,不需要登入,不採用任何身分驗證(即使帶了
    Authorization header 也不解析)。

    回應改回傳完整活動內容(跟 ``GET /api/events/{id}`` 同一份
    ``EventDetailSerializer`` 輸出),不是只回傳新建 response 的 ``id``——
    使用者要求前端送出投票後能立即拿到最新彙整結果直接渲染,不用另外再打一次
    ``GET``(design.md D16)。寫入完成後重新查一次
    ``_event_with_responses_queryset()``,用 prefetch 過的 queryset 序列化,
    避免 N+1。
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, id):
        event = _get_event_or_404(id)
        _check_participation_preconditions(event)

        serializer = ParticipantResponseCreateSerializer(
            data=request.data, context={"event": event}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        event = _get_event_or_404(id, queryset=_event_with_responses_queryset())
        response_serializer = EventDetailSerializer(event, context={"request": request})
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


def _hash_participant_access_token(token_value):
    """回傳存取憑證明文的 SHA-256 hex digest。

    不加 salt——比照 ``apps.accounts.services._hash_refresh_token`` 的既有作法
    （見該函式 docstring）：``secrets.token_urlsafe`` 產生的高熵字串本身不可
    窮舉，跟手機末三碼（見 ``design.md`` D1）那種低熵輸入需要 per-record salt
    的情況不同。
    """
    return hashlib.sha256(token_value.encode()).hexdigest()


class ParticipantResponseVerifyView(APIView):
    """``POST /api/events/{id}/responses/verify`` — 任何人(含未登入)透過活動
    分享連結核對身分(暱稱＋手機末三碼)。完全公開,不需要登入,不採用任何身分
    驗證,同 ``ParticipantResponseCreateView``。

    核對成功核發一組一次性存取憑證(明文只在這次回應回傳,DB 只存雜湊值,見
    ``_hash_participant_access_token``),供後續 ``PATCH`` 修改投票使用。查無
    此暱稱、或暱稱存在但手機末三碼不符,皆回同一個 401
    ``IDENTITY_VERIFICATION_FAILED``(design.md D3),不讓回應內容洩漏兩者的
    差異——因此這裡刻意不呼叫 ``get_object_or_404`` 之類會分岔出不同錯誤訊息
    的寫法,兩個失敗分支共用同一段 ``raise``。
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, id):
        event = _get_event_or_404(id)
        _check_participation_preconditions(event)

        serializer = ParticipantResponseVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        nickname = serializer.validated_data["nickname"]
        phone_last_three = serializer.validated_data["phoneLastThree"]

        participant_response = event.responses.filter(nickname=nickname).first()
        if participant_response is not None:
            phone_matches = check_password(
                phone_last_three, participant_response.phone_last_three_hash
            )
        else:
            check_password(phone_last_three, _DUMMY_PHONE_HASH_FOR_TIMING)
            phone_matches = False

        if participant_response is None or not phone_matches:
            raise ApiError(
                "暱稱或手機末三碼不正確",
                code="IDENTITY_VERIFICATION_FAILED",
                status_code=401,
            )

        plaintext_token = secrets.token_urlsafe(32)
        expires_at = timezone.now() + PARTICIPANT_ACCESS_TOKEN_TTL
        ParticipantResponseAccessToken.objects.create(
            response=participant_response,
            token_hash=_hash_participant_access_token(plaintext_token),
            expires_at=expires_at,
        )

        return Response(
            {
                "accessToken": plaintext_token,
                "expiresAt": expires_at,
                "nickname": participant_response.nickname,
                "email": participant_response.email,
                "slotAvailabilities": [
                    {
                        "slotId": str(availability.slot_id),
                        "availability": availability.availability,
                    }
                    for availability in participant_response.slot_availabilities.all()
                ],
            },
            status=status.HTTP_200_OK,
        )


class ParticipantResponseDetailView(APIView):
    """``PATCH /api/events/{id}/responses/{responseId}`` — 參與者憑
    ``POST .../verify`` 核發的一次性存取憑證修改候選時段選擇。完全公開,不需要
    登入,不採用任何身分驗證(同另外兩支參與者端點)——身分驗證改用請求 body
    裡的 ``accessToken`` 完成,見 ``patch()``。

    ``accessToken`` 刻意不放進 ``ParticipantResponsePatchSerializer`` 宣告
    (見該類別 docstring),這裡直接從 ``request.data`` 取值、比對雜湊。

    回應同 ``ParticipantResponseCreateView``,改回傳完整活動內容(跟
    ``GET /api/events/{id}`` 同一份 ``EventDetailSerializer`` 輸出),見
    design.md D16。
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def patch(self, request, id, responseId):
        event = _get_event_or_404(id)

        # 先驗證存取憑證(design.md D2):存在、未過期、未使用過、關聯的
        # response 對得上 URL 的 responseId(同時隱含對得上這個活動)——任一
        # 不符皆回同一個 401 ACCESS_TOKEN_INVALID,不細分原因(design.md D2)。
        # 這一步刻意排在共用前置條件檢查與 slot 驗證之前:token 本身無效時,
        # 不該讓請求者靠著觀察 410/409/400 的差異推敲出活動目前的狀態。
        token_value = request.data.get("accessToken")
        token_record = None
        if isinstance(token_value, str) and token_value:
            token_record = (
                ParticipantResponseAccessToken.objects.select_related("response")
                .filter(token_hash=_hash_participant_access_token(token_value))
                .first()
            )

        now = timezone.now()
        if (
            token_record is None
            or token_record.used_at is not None
            or token_record.expires_at <= now
            or token_record.response_id != responseId
            or token_record.response.event_id != event.id
        ):
            raise ApiError(
                "存取憑證無效、已過期或已被使用，請重新核對身分",
                code="ACCESS_TOKEN_INVALID",
                status_code=401,
            )

        # 前置條件(連結未失效／狀態進行中／未過投票截止時間)與候選時段驗證
        # 失敗都不消費 token——讓使用者修正請求後,原本那組 token 仍可重試。
        _check_participation_preconditions(event)

        serializer = ParticipantResponsePatchSerializer(
            data=request.data, context={"event": event}
        )
        serializer.is_valid(raise_exception=True)
        availabilities = serializer.validated_data["slotAvailabilities"]

        participant_response = token_record.response
        with transaction.atomic():
            # Compare-and-swap:UPDATE ... WHERE used_at IS NULL AND expires_at
            # > 當下 才是真正保證一次性消費、且消費當下仍未過期的地方——上面
            # 那段早期檢查只是為了快速失敗,兩個請求帶著同一個 token 同時通過
            # 早期檢查、同時走到這裡時,DB 層級只有一個 UPDATE 能真的把
            # used_at 從 NULL 改掉,affected row 數可拿來判斷輸贏,不能只憑
            # Python 物件裡讀到的舊值(code-review 抓到:純 .save() 沒有
            # WHERE 條件,兩個請求會都成功覆寫)。expires_at 也要在同一個
            # UPDATE 裡重新核對、用重新取得的當下時間——否則 token 若剛好在
            # 早期檢查通過之後、這個 UPDATE 執行之前的極短空檔到期,早期檢查
            # 用的是舊的 now,不會抓到,會讓已過期的 token 仍成功消費
            # (Codex 二次審查抓到)。
            claimed_at = timezone.now()
            claimed = ParticipantResponseAccessToken.objects.filter(
                pk=token_record.pk, used_at__isnull=True, expires_at__gt=claimed_at
            ).update(used_at=claimed_at)
            if claimed == 0:
                raise ApiError(
                    "存取憑證無效、已過期或已被使用，請重新核對身分",
                    code="ACCESS_TOKEN_INVALID",
                    status_code=401,
                )
            # 每次更新都是完整覆蓋(design.md D4 2026-09-21 修訂③,請求本身
            # 已由 serializer 驗證涵蓋該活動全部候選時段、恰好各一次)——先刪
            # 除既有表態列、再整批重建,比逐筆 update_or_create 簡單,不用比對
            # 哪些筆要新增/更新/刪除,見 design.md D4a。
            ParticipantResponseSlotAvailability.objects.filter(
                response=participant_response
            ).delete()
            ParticipantResponseSlotAvailability.objects.bulk_create(
                [
                    ParticipantResponseSlotAvailability(
                        response=participant_response,
                        slot_id=item["slotId"],
                        availability=item["availability"],
                    )
                    for item in availabilities
                ]
            )

        event = _get_event_or_404(id, queryset=_event_with_responses_queryset())
        response_serializer = EventDetailSerializer(event, context={"request": request})
        return Response(response_serializer.data, status=status.HTTP_200_OK)
