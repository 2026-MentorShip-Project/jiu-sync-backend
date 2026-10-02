import hashlib
import logging
import secrets
from datetime import timedelta

import redis
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.db.models import Count, Max, Prefetch, Q
from django.utils import dateparse, timezone
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from apps.notifications.tasks import (
    send_event_cancelled_email,
    send_event_created_email,
    send_event_finalized_email,
    send_event_reopened_email,
)
from config.exceptions import ApiError, Gone
from config.logging import log_event_on_commit

from .authentication import OptionalJWTAuthentication
from .lifecycle import compute_display_status
from .models import (
    Comment,
    Event,
    ParticipantResponse,
    ParticipantResponseAccessToken,
    ParticipantResponseSlotAvailability,
)
from .serializers import (
    CommentCreateSerializer,
    CommentSerializer,
    EventCreateSerializer,
    EventDetailSerializer,
    EventFinalizeSerializer,
    EventPatchSerializer,
    EventReopenSerializer,
    EventSummarySerializer,
    ParticipantResponseCreateSerializer,
    ParticipantResponsePatchSerializer,
    ParticipantResponseVerifySerializer,
)

# ParticipantResponseAccessToken 的效期,核發後固定 30 分鐘(design.md D2)。
PARTICIPANT_ACCESS_TOKEN_TTL = timedelta(minutes=30)

# GET /api/events/{id}/comments 固定每頁筆數,不開放前端指定 limit
# （add-comment-pagination design.md D4)。
COMMENT_PAGE_SIZE = 10

# POST /api/events/{id}/comments 留言防洗版鎖的鎖定秒數（add-comment-rate-limit
# design.md D3）。同一來源 IP 對同一活動,鎖定期間內再次留言會被拒絕。
COMMENT_RATE_LIMIT_TTL_SECONDS = 2

# 暱稱查無資料時,仍對這個固定雜湊值跑一次 check_password,讓「暱稱不存在」與
# 「暱稱存在但手機碼錯誤」兩種失敗耗費的時間趨於一致——design.md D3 只保證
# 回應「內容」不洩漏差異,若略過雜湊比對直接短路,兩種失敗的回應時間仍可被用來
# 側錄暱稱是否存在(code-review 抓到)。模組載入時算一次即可,不用每次請求
# 重新雜湊。
_DUMMY_PHONE_HASH_FOR_TIMING = make_password("000")

logger = logging.getLogger(__name__)


def _schedule_notification(task, event_id, *task_args):
    """把通知信 task 排進 ``transaction.on_commit()``，並且吞掉排入/執行當下
    拋出的例外，只記 log，不讓它往上炸穿整個 view。

    ``task_args``:這次 transition 寫入 DB 的識別值（例如 ``finalized_at``），
    原樣轉傳給 task，讓 task 執行時能核對自己是否已被後續 transition 蓋過
    （design.md D10，過期 task 判斷）。

    使用者實測發現:本機 ``CELERY_TASK_ALWAYS_EAGER=True`` 時，
    ``.delay()`` 在 ``on_commit`` 觸發當下同步執行，若寄信失敗（例如 email
    backend 設定錯誤），例外會直接讓這次 API 回應變成 500——即使真正的
    狀態轉換（``EventFinalizeView``/``EventCancelView`` 的 CAS ``UPDATE``）
    早在 ``on_commit`` 觸發前就已經 commit 成功。通知信寄送失敗不該讓一個
    已經成功的動作看起來像失敗（design.md Risks 原本就講明這是刻意的設計
    意圖，只是先前沒有真的做防護）。正式環境用真的 Celery worker 時，這層
    防護仍然有意義：``.delay()`` 本身（把訊息放進 Redis 佇列）理論上也可能
    因為 broker 連線問題丟例外，同樣不該讓 API 回應失敗。
    """

    def _run():
        try:
            task.delay(event_id, *task_args)
        except Exception:
            logger.exception(
                "Failed to schedule notification task %s for event %s",
                task.name,
                event_id,
            )

    transaction.on_commit(_run)


def _log_event_transition(event_name, event_id, user):
    """主揪對活動的狀態變更事件 log,commit 後輸出(add-observability-stack D11)。
    只帶 id,不帶活動標題與 email。"""
    log_event_on_commit(
        logger,
        event_name,
        event_name.replace(".", " "),
        event_id=str(event_id),
        user_id=str(user.id),
    )


def _get_client_ip(request):
    """回傳這次請求的來源 client IP(add-comment-rate-limit design.md D4)。

    正式環境下每個請求都經過 nginx，``REMOTE_ADDR`` 固定是 nginx 自己的位址、
    不是真正的使用者來源，必須讀 nginx 轉發的 ``X-Forwarded-For``（取第一個
    逗號分隔值）才拿得到真實 client IP。本機開發環境沒有 nginx，沒有這個
    header 時退回 ``REMOTE_ADDR``。

    外部依賴（不在本 repo 範圍，見 design.md D4）：這個 helper 的前提是 nginx
    設定檔有正確帶上 ``X-Forwarded-For``；沒有的話正式環境會一律拿到 nginx
    自己的位址，導致所有使用者共用同一把鎖。
    """
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


_comment_rate_limit_redis_client = None
_comment_rate_limit_redis_client_url = None


def _get_comment_rate_limit_redis_client():
    """回傳留言防洗版鎖專用的 Redis client，快取重用而不是每次請求都重新
    建立（code-review 抓到：熱路徑上每個 POST 都重新
    ``redis.Redis.from_url()`` 會重複付出建立 connection pool 的成本）。

    只有 ``settings.COMMENT_RATE_LIMIT_REDIS_URL`` 改變時才重建——測試會用
    ``override_settings`` 指向不存在的位址模擬連線失敗（D7），若整支快取死
    在模組載入當下的第一份 client，測試改的 setting 永遠不會生效。
    """
    global _comment_rate_limit_redis_client, _comment_rate_limit_redis_client_url
    url = settings.COMMENT_RATE_LIMIT_REDIS_URL
    if (
        _comment_rate_limit_redis_client is None
        or _comment_rate_limit_redis_client_url != url
    ):
        _comment_rate_limit_redis_client = redis.Redis.from_url(
            url, socket_connect_timeout=1, socket_timeout=1
        )
        _comment_rate_limit_redis_client_url = url
    return _comment_rate_limit_redis_client


def _try_acquire_comment_rate_limit_lock(event_id, client_ip):
    """留言防洗版鎖(add-comment-rate-limit design.md D2/D3)。

    用單一原子的 Redis ``SET key 1 NX EX 2`` 當「檢查是否鎖定中」與「上鎖」
    合一的單一動作——回傳成功即代表這次請求搶到鎖、可以繼續寫入 DB；回傳
    失敗(key 已存在)即代表目前在鎖定中，呼叫端應直接拒絕、不寫入。不拆成
    「先 GET 檢查」再「另外 SET」兩步，那樣兩個近乎同時抵達的併發請求會都
    通過檢查階段、都寫入、都上鎖，一樣是 check-then-act 的 TOCTOU 問題，達不
    到防洗版效果(design.md D3)。

    呼叫端必須在 ``serializer.is_valid()`` 成功之後、``serializer.save()`` 之
    前呼叫這個函式(design.md D2)——驗證失敗（400）不該消耗鎖；但也不能等
    ``save()`` 也成功了才上鎖，否則兩個近乎同時抵達、都通過驗證的請求會在鎖
    生效前搶先都執行完 ``save()``，變成兩則都寫入成功，完全防不了連點洗版
    （這正是這個 change 要擋下的核心情境）。

    Redis 例外一律 fail-open(design.md D7)：防洗版是附加保護機制，不應該
    因為 Redis 本身的基礎設施問題拖垮留言這個核心功能，只記一筆 warning
    log、視同搶到鎖，讓呼叫端繼續寫入 DB。除了連線類例外
    (``redis.exceptions.RedisError``，涵蓋 timeout／connection refused 等)
    也一併捕捉 ``ValueError``——``redis.Redis.from_url()`` 對不合法的 URL
    scheme（例如 ``COMMENT_RATE_LIMIT_REDIS_URL`` 設定錯誤）是拋
    ``ValueError`` 而不是 ``RedisError``，只捕捉後者會讓設定錯誤直接讓每
    個留言請求都 500，違反 D7 fail-open 的初衷（code-review 抓到）。

    ``socket_connect_timeout``／``socket_timeout`` 給得很短——fail-open 情境下
    仍要盡快讓請求正常往下走，不能讓一個掛掉的 Redis 拖住整個請求的回應
    時間。
    """
    key = f"comment_rl:{event_id}:{client_ip}"
    try:
        client = _get_comment_rate_limit_redis_client()
        acquired = client.set(key, 1, nx=True, ex=COMMENT_RATE_LIMIT_TTL_SECONDS)
    except (redis.exceptions.RedisError, ValueError):
        logger.warning(
            "Comment rate limit Redis unavailable (event=%s, ip=%s), failing open",
            event_id,
            client_ip,
            exc_info=True,
        )
        return True
    return bool(acquired)


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
    D16;以及 ``EventFinalizeView``/``EventCancelView``)。
    ``prefetch_related("slots", ...)`` 避免 ``get_responses()``/
    ``get_slotSummary()``(D17)各自造成 N+1,抽成共用函式避免多處重複一次
    一模一樣的 queryset 組合。

    ``responses`` 用 ``Prefetch`` 而不是純字串 ``"responses__slot_availabilities"``
    ——需要把「排除已軟刪除的投票紀錄」（``add-event-lifecycle`` design.md D6）
    做進 prefetch queryset 本身：直接在 serializer 端對 ``event.responses``
    多加一次 ``.filter()`` 不會命中 prefetch cache（只有原封不動的 ``.all()``
    才吃快取），會變成另開一條 N+1 query，等於白做這層防護。
    """
    # restaurant_selection:``EventDetailSerializer.selectedRestaurant`` 讀取的
    # 反向 OneToOne(由 apps.recommendations 定義),JOIN 進同一個查詢,不另外查。
    return Event.objects.select_related(
        "owner", "final_slot", "restaurant_selection"
    ).prefetch_related(
        "slots",
        Prefetch(
            "responses",
            queryset=ParticipantResponse.objects.filter(
                deleted_at__isnull=True
            ).prefetch_related("slot_availabilities"),
        ),
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

    建立成功後比照 ``EventFinalizeView``/``EventCancelView``/``EventReopenView``
    既有的機制,透過 ``_schedule_notification`` 非同步寄一封含分享連結的通知
    信到主揪本人的 Google 帳號信箱（``event.host_email``,design.md D1，
    `add-event-created-email`）。不像另外三支需要傳入 transition 當下的時間
    戳做「過期 task」判斷（design.md D10）——建立活動對同一個 ``event.id``
    只會發生一次,不存在被後續動作蓋過的疑慮（design.md D2）,所以只傳
    ``event.id``。
    """

    def post(self, request):
        serializer = EventCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # host_email 一律取自 request.user.email,不採信請求 body 中任何
        # hostEmail 欄位——EventCreateSerializer 根本不宣告該欄位。
        event = serializer.save(owner=request.user, host_email=request.user.email)
        _schedule_notification(send_event_created_email, event.id)
        _log_event_transition("event.created", event.id, request.user)

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
    提交初次投票。不需要登入；選擇性解析 JWT，讓主揪可使用自己的主揪暱稱。

    回應改回傳完整活動內容(跟 ``GET /api/events/{id}`` 同一份
    ``EventDetailSerializer`` 輸出),不是只回傳新建 response 的 ``id``——
    使用者要求前端送出投票後能立即拿到最新彙整結果直接渲染,不用另外再打一次
    ``GET``(design.md D16)。寫入完成後重新查一次
    ``_event_with_responses_queryset()``,用 prefetch 過的 queryset 序列化,
    避免 N+1。
    """

    permission_classes = [AllowAny]
    authentication_classes = [OptionalJWTAuthentication]

    def post(self, request, id):
        event = _get_event_or_404(id)
        _check_participation_preconditions(event)

        serializer = ParticipantResponseCreateSerializer(
            data=request.data, context={"event": event, "request": request}
        )
        serializer.is_valid(raise_exception=True)
        participant_response = serializer.save()
        log_event_on_commit(
            logger,
            "event.response_created",
            "participant response created",
            event_id=str(event.id),
            response_id=str(participant_response.id),
        )

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
    ``_hash_participant_access_token``),供後續 ``PATCH`` 修改投票使用。回應
    一併回傳該筆投票的 ``id``——前端組
    ``PATCH /api/events/{id}/responses/{responseId}`` 的 URL 需要這個值,
    換裝置或清除本地儲存後,``verify`` 是前端唯一能重新拿到它的來源(見
    design.md D18)。查無此暱稱、或暱稱存在但手機末三碼不符,皆回同一個 401
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
                "id": participant_response.id,
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
            # 上面只動了子表(ParticipantResponseSlotAvailability),完全沒碰
            # ParticipantResponse 本身任何欄位，auto_now 的 updated_at 不會
            # 自動蓋章。add-event-poll 的輪詢端點需要靠這個欄位分辨「有投票
            # 被修改」（不只是「有新投票」），這裡補一次輕量 UPDATE，沿用同一個
            # 已經算好的 claimed_at，不重新呼叫 timezone.now()。
            ParticipantResponse.objects.filter(pk=participant_response.pk).update(
                updated_at=claimed_at
            )

        event = _get_event_or_404(id, queryset=_event_with_responses_queryset())
        response_serializer = EventDetailSerializer(event, context={"request": request})
        return Response(response_serializer.data, status=status.HTTP_200_OK)


def _encode_comment_cursor(comment):
    """組出 keyset pagination 的 cursor(add-comment-pagination design.md D2)。
    格式 ``{created_at.isoformat()}_{id}``,不做 base64 包裝——cursor 不是
    敏感資訊,純粹是查詢起點,base64 只會增加除錯難度。用底線分隔是因為
    ``created_at.isoformat()`` 本身不含底線字元,可以安全用
    ``rsplit("_", 1)`` 還原成兩段。"""
    return f"{comment.created_at.isoformat()}_{comment.id}"


def _decode_comment_cursor(cursor):
    """解析 ``_encode_comment_cursor`` 產生的 cursor,回傳
    ``(created_at, id)`` tuple；格式不合法(缺底線、``created_at`` 不是合法
    ISO 字串、id 段落為空)一律回傳 ``None``,不拋例外——呼叫端把 ``None``
    視同沒有帶 cursor(design.md「風險」段落:cursor 被竄改成不存在或格式
    不合法的值,查詢應該優雅退化,不能 500)。"""
    if not cursor:
        return None
    try:
        created_at_part, comment_id = cursor.rsplit("_", 1)
    except ValueError:
        return None
    created_at = dateparse.parse_datetime(created_at_part)
    if created_at is None or not comment_id:
        return None
    return created_at, comment_id


class CommentListCreateView(APIView):
    """``GET``/``POST /api/events/{id}/comments`` — 活動留言板，任何人（含未
    登入）皆可查詢、留言。完全公開，不需要登入，不採用任何身分驗證，同三支
    參與者端點；且完全獨立於 ``ParticipantResponse``（design.md D2），不需要
    先投票或核對身分。

    前提條件刻意只檢查「連結未失效」（``_display_status_or_410``），不呼叫
    ``_check_participation_preconditions``——活動狀態（進行中／已定案／已
    取消）與投票截止時間皆不影響能否留言，這點跟參與者投票三支端點明確不同
    （design.md D5）。

    ``POST`` 額外套用留言防洗版鎖（add-comment-rate-limit design.md D1/D2/D3）
    ：以「來源 IP + 活動 id」為 key，驗證通過後、寫入 DB 前嘗試搶
    2 秒的 Redis 鎖，搶不到回 429 ``COMMENT_RATE_LIMITED``、不寫入。
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, id):
        event = _get_event_or_404(id)
        _display_status_or_410(event)
        # 已軟刪除的留言(design.md D9)排除在外——對查詢者而言就是不存在,
        # 也不計入分頁筆數(add-comment-pagination design.md tasks 2.1 ⑥)。
        comments = event.comments.filter(deleted_at__isnull=True)

        # Cursor 分頁(keyset pagination,不是 offset——add-comment-pagination
        # design.md D3):cursor 解析失敗一律視同沒有帶 cursor,優雅退化回傳
        # 最新一頁,不 500。用複合條件(created_at 為主、id 為次要排序鍵)是
        # 為了處理兩則留言 created_at 完全相同的邊界情況。
        cursor = _decode_comment_cursor(request.query_params.get("cursor"))
        if cursor is not None:
            cursor_created_at, cursor_id = cursor
            comments = comments.filter(
                Q(created_at__lt=cursor_created_at)
                | Q(created_at=cursor_created_at, id__lt=cursor_id)
            )

        # 固定每頁 10 則,不開放前端指定 limit(design.md D4)。由新到舊排序
        # ——跟 Comment.Meta.ordering(遞增,由舊到新)方向相反,這支端點的
        # 回應順序刻意反過來(design.md Context)。
        page = list(comments.order_by("-created_at", "-id")[:COMMENT_PAGE_SIZE])

        # 取滿一頁才代表可能還有更舊的資料;不滿一頁代表已經是最後一批,
        # nextCursor 為 None（design.md D1，不額外加 hasMore）。
        next_cursor = (
            _encode_comment_cursor(page[-1])
            if len(page) == COMMENT_PAGE_SIZE
            else None
        )

        serializer = CommentSerializer(page, many=True)
        return Response({"comments": serializer.data, "nextCursor": next_cursor})

    def post(self, request, id):
        event = _get_event_or_404(id)
        _display_status_or_410(event)

        serializer = CommentCreateSerializer(data=request.data, context={"event": event})
        serializer.is_valid(raise_exception=True)

        # 上鎖時機:驗證通過之後、寫入 DB 之前（design.md D2）——驗證失敗
        # （400）不會走到這裡，不消耗鎖；但也不能等 serializer.save() 也成功
        # 了才上鎖，否則兩個近乎同時抵達、都通過驗證的請求會在鎖生效前搶先
        # 都寫入成功，完全防不了連點洗版。搶不到鎖直接 429，不呼叫
        # serializer.save()。
        client_ip = _get_client_ip(request)
        if not _try_acquire_comment_rate_limit_lock(event.id, client_ip):
            raise ApiError(
                "留言太頻繁，請稍後再試",
                code="COMMENT_RATE_LIMITED",
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        comment = serializer.save()

        response_serializer = CommentSerializer(comment)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


class CommentDetailView(APIView):
    """``DELETE /api/events/{id}/comments/{commentId}`` — 已登入且為該活動擁
    有者（主揪）可刪除一則留言（design.md D9）。比照 ``EventDetailView.patch()``
    的擁有者權限檢查模式：``IsAuthenticated`` + 全域預設的 ``JWTAuthentication``，
    非擁有者一律 403 ``Forbidden``。

    軟刪除，不做實體刪除——寫入 ``deleted_at``，資料庫紀錄保留。查無該留言、
    已被刪除過、或不屬於 URL 指定的活動，皆回 404 ``COMMENT_NOT_FOUND``，不
    細分原因（這是自然結果，不是刻意的側通道防禦，見 design.md D9）。
    """

    def delete(self, request, id, commentId):
        event = _get_event_or_404(id)
        if request.user != event.owner:
            raise PermissionDenied("僅活動擁有者可刪除留言")

        # Compare-and-swap:UPDATE ... WHERE deleted_at IS NULL 才是真正保證
        # 「已刪除的留言不能再被刪一次」的地方——純 .save() 沒有 WHERE 條件,
        # 兩個並發請求對著同一則留言的 SELECT 都會通過 deleted_at__isnull=True
        # 的檢查,都會成功寫入,都回 204(code-review 抓到,同款問題先前已在
        # ParticipantResponseDetailView.patch() 的 token 消費修過一次,見
        # design.md D9)。affected 用來判斷這次請求是否真的是「贏家」。
        affected = Comment.objects.filter(
            pk=commentId, event=event, deleted_at__isnull=True
        ).update(deleted_at=timezone.now())
        if affected == 0:
            raise ApiError(
                "找不到此留言，可能已被刪除或不存在",
                code="COMMENT_NOT_FOUND",
                status_code=404,
            )

        return Response(status=status.HTTP_204_NO_CONTENT)


class EventFinalizeView(APIView):
    """``POST /api/events/{id}/finalize`` — 已登入且為活動擁有者的主揪，將一筆
    進行中（``active``）的活動定案（design.md D3，`add-event-lifecycle`）。

    擁有者權限檢查比照 ``EventDetailView.patch()``：非擁有者 403
    ``FORBIDDEN``，未登入 401。狀態轉換用 compare-and-swap（design.md D7）
    ——`UPDATE ... WHERE status='active'` 才是真正保證「兩個並發定案請求只有
    一個成功」的地方，純 `.save()` 沒有 `WHERE` 條件做不到。

    刻意不呼叫 ``_display_status_or_410``（design.md 2026-09-24 修訂，
    `add-event-reopen` 的 code-review 抓到）：連結失效（``link_expired``）
    是給參與者這類公開／匿名端點用的「這個連結已經死了，別再互動」概念，
    不該套用在主揪對自己活動的管理動作上——且 `finalize` 的唯一可執行前提
    狀態（`active`）本來就不可能算出 `link_expired`（見
    `lifecycle.compute_display_status` 的 `active` 分支），這裡拿掉純粹是
    為了跟 `EventCancelView`/`EventReopenView` 三支保持一致，不是行為改變。
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, id):
        # 輕量 queryset——這裡只需要 owner/status/slots.exists() 檢查，不需要
        # 完整的 responses/slotSummary prefetch，那組留到成功後最終序列化
        # 回應時才查一次（code-review 抓到：原本兩處都用重量 queryset，成功
        # 路徑白白多付一次 prefetch 成本，比照 ParticipantResponseCreateView
        # 既有的輕重分離寫法）。
        event = _get_event_or_404(id)
        if request.user != event.owner:
            raise PermissionDenied("僅活動擁有者可定案")

        serializer = EventFinalizeSerializer(data=request.data, context={"event": event})
        serializer.is_valid(raise_exception=True)

        claimed_at = timezone.now()
        affected = Event.objects.filter(pk=event.id, status=Event.Status.ACTIVE).update(
            status=Event.Status.FINALIZED,
            final_slot_id=serializer.validated_data["finalSlotId"],
            final_note=serializer.validated_data.get("finalNote") or None,
            finalized_at=claimed_at,
            updated_at=claimed_at,
        )
        if affected == 0:
            current_status = Event.objects.values_list("status", flat=True).get(
                pk=event.id
            )
            if current_status == Event.Status.CANCELLED:
                raise ApiError(
                    "活動已取消，無法定案", code="EVENT_ALREADY_CANCELLED", status_code=409
                )
            raise ApiError(
                "活動已經定案過了", code="EVENT_ALREADY_FINALIZED", status_code=409
            )

        event = _get_event_or_404(id, queryset=_event_with_responses_queryset())
        _schedule_notification(send_event_finalized_email, event.id, event.finalized_at)
        _log_event_transition("event.finalized", event.id, request.user)

        response_serializer = EventDetailSerializer(event, context={"request": request})
        return Response(response_serializer.data, status=status.HTTP_200_OK)


class EventCancelView(APIView):
    """``POST /api/events/{id}/cancel`` — 已登入且為活動擁有者的主揪，取消一筆
    進行中（``active``）或已定案（``finalized``）的活動（design.md D4，
    `add-event-lifecycle`）。已經是 ``cancelled`` 的活動不可再次取消。

    取消成功會把既有的參與者投票紀錄全數軟刪除（design.md D6）——跟狀態轉換
    包在同一個 ``transaction.atomic()`` 裡，要嘛兩者一起成功、要嘛一起回滾。

    刻意不呼叫 ``_display_status_or_410``（design.md 2026-09-24 修訂）——
    這是 code-review 在審 `add-event-reopen` 時抓到的真實 bug，`cancel`
    這支跟它同款：`cancel` 的可執行前提狀態包含 `finalized`，一筆定案超過
    7 天（不是聚會超過 7 天，是「定案這個動作」超過 7 天）的活動
    `displayStatus` 會被算成 `link_expired`，導致主揪永遠無法取消一筆
    「已經定案一段時間、但聚會可能還沒發生」的活動。拿掉這層檢查後，
    連結是否失效不再影響主揪能不能取消自己的活動。
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, id):
        # 輕量 queryset，理由同 EventFinalizeView（code-review 抓到）。
        event = _get_event_or_404(id)
        if request.user != event.owner:
            raise PermissionDenied("僅活動擁有者可取消活動")

        claimed_at = timezone.now()
        with transaction.atomic():
            affected = Event.objects.filter(
                pk=event.id, status__in=[Event.Status.ACTIVE, Event.Status.FINALIZED]
            ).update(
                status=Event.Status.CANCELLED,
                cancelled_at=claimed_at,
                # 取消一筆已定案的活動時，這三個欄位要一併清空——code-review
                # 抓到：原本沒清，status="cancelled" 卻仍回傳舊的
                # finalSlotId/finalNote，前端會看到自相矛盾的「已取消但也
                # 已定案」畫面。
                final_slot=None,
                final_note=None,
                finalized_at=None,
                updated_at=claimed_at,
            )
            if affected == 0:
                raise ApiError(
                    "活動已經取消過了", code="EVENT_ALREADY_CANCELLED", status_code=409
                )

            ParticipantResponse.objects.filter(
                event=event, deleted_at__isnull=True
            ).update(deleted_at=claimed_at)

        event = _get_event_or_404(id, queryset=_event_with_responses_queryset())
        _schedule_notification(send_event_cancelled_email, event.id, event.cancelled_at)
        _log_event_transition("event.cancelled", event.id, request.user)

        response_serializer = EventDetailSerializer(event, context={"request": request})
        return Response(response_serializer.data, status=status.HTTP_200_OK)


class EventReopenView(APIView):
    """``POST /api/events/{id}/reopen`` — 已登入且為活動擁有者的主揪，將一筆
    已定案（``finalized``）的活動重新開放為進行中（``active``）（design.md
    D1，`add-event-reopen`）。``active``/``cancelled`` 狀態皆拒絕，統一回
    409 ``EVENT_NOT_FINALIZED``——不像 `finalize` 需要區分兩種「不能執行」的
    來源，這裡只有一種允許的前置狀態，拒絕原因永遠相同。

    既有參與者投票紀錄完全不動——``finalize`` 不會軟刪除任何
    ``ParticipantResponse``（只有 ``cancel`` 才會），所以這條路徑上沒有
    資料需要復原或清理，CAS 只需要動 ``Event`` 自己的欄位。

    刻意不呼叫 ``_display_status_or_410``（design.md 2026-09-24 修訂，
    code-review 抓到的真實 bug）：`reopen` 唯一的可執行前提狀態就是
    `finalized`，而 `finalized` 活動一旦超過 7 天就會被
    `compute_display_status` 算成 `link_expired`——這正是 `reopen`
    最主要、甚至可能是唯一有意義的使用情境（主揪很久以前定案了，現在想
    重開），原本的檢查順序會讓這個功能對它自己的核心用途完全用不了，
    每次都先被 410 擋下，永遠碰不到下面真正的 CAS 判斷。
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, id):
        event = _get_event_or_404(id)
        if request.user != event.owner:
            raise PermissionDenied("僅活動擁有者可重新開放投票")

        serializer = EventReopenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        affected = Event.objects.filter(
            pk=event.id, status=Event.Status.FINALIZED
        ).update(
            status=Event.Status.ACTIVE,
            response_deadline=serializer.validated_data["responseDeadline"],
            final_slot=None,
            final_note=None,
            finalized_at=None,
            updated_at=timezone.now(),
        )
        if affected == 0:
            raise ApiError(
                "活動目前不是已定案狀態，無法重新開放投票",
                code="EVENT_NOT_FINALIZED",
                status_code=409,
            )

        event = _get_event_or_404(id, queryset=_event_with_responses_queryset())
        _schedule_notification(
            send_event_reopened_email, event.id, event.response_deadline
        )
        _log_event_transition("event.reopened", event.id, request.user)

        response_serializer = EventDetailSerializer(event, context={"request": request})
        return Response(response_serializer.data, status=status.HTTP_200_OK)


class EventPollView(APIView):
    """``GET /api/events/{id}/poll`` — 公開（不需登入）的輕量輪詢端點，前端
    每隔固定秒數（例如 10 秒）查詢一筆活動有沒有變化，不含 ``responses``/
    ``slotSummary``/``comments`` 明細內容（add-event-poll D1）。前端偵測到
    回應值跟上次不同才另外打 ``GET /api/events/{id}``／
    ``GET /api/events/{id}/comments`` 取得完整內容。

    跟 ``GET /api/events/{id}`` 同一批對象、同一套連結失效規則（D3）：
    ``AllowAny`` + 空 ``authentication_classes``（不需要 ``isOwner`` 判斷，
    不用 ``EventDetailView`` 的 ``OptionalJWTAuthentication``），
    ``_display_status_or_410`` 算出 ``link_expired`` 時回 410。

    ``responseCount``/``commentCount`` 只算未軟刪除的紀錄（D4），
    ``latestResponseAt`` 用 ``MAX(updated_at)`` 而非 ``MAX(created_at)``——
    ``PATCH .../responses/{responseId}`` 改票時也會更新 ``updated_at``
    （D2），才能同時反映「新投票」與「改票」兩種變化。
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, id):
        event = _get_event_or_404(id)
        display_status = _display_status_or_410(event)

        response_agg = ParticipantResponse.objects.filter(
            event=event, deleted_at__isnull=True
        ).aggregate(count=Count("id"), latest=Max("updated_at"))
        comment_agg = Comment.objects.filter(
            event=event, deleted_at__isnull=True
        ).aggregate(count=Count("id"), latest=Max("created_at"))

        return Response(
            {
                "status": event.status,
                "displayStatus": display_status,
                "eventUpdatedAt": event.updated_at,
                "responseCount": response_agg["count"],
                "latestResponseAt": response_agg["latest"],
                "commentCount": comment_agg["count"],
                "latestCommentAt": comment_agg["latest"],
            }
        )
