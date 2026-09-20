import unicodedata

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import serializers

from .lifecycle import compute_display_status
from .models import Event, Slot

HOST_NICKNAME_MAX_WEIGHTED_LENGTH = 40
MIN_SLOTS = 1
MAX_SLOTS = 20
EVENT_ID_COLLISION_MAX_ATTEMPTS = 3


def _weighted_length(value):
    """中日韓(CJK)字元計 2、其餘字元計 1 的加權長度。

    以 ``unicodedata.east_asian_width`` 判斷:Wide/Fullwidth 視為 CJK 字元。
    """
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in value)


def _validate_host_nickname_weighted_length(value):
    """`hostNickname` 加權長度驗證,`EventCreateSerializer`/`EventPatchSerializer` 共用。"""
    if _weighted_length(value) > HOST_NICKNAME_MAX_WEIGHTED_LENGTH:
        raise serializers.ValidationError(
            f"主揪暱稱加權長度(CJK 字元計 2、其餘計 1)不得超過"
            f" {HOST_NICKNAME_MAX_WEIGHTED_LENGTH}",
            code="HOST_NICKNAME_TOO_LONG",
        )
    return value


def _validate_response_deadline_in_future(value):
    """`responseDeadline` 須晚於當下驗證,`EventCreateSerializer`/`EventPatchSerializer` 共用。"""
    if value <= timezone.now():
        raise serializers.ValidationError(
            "投票截止時間必須晚於目前時間", code="DEADLINE_IN_PAST"
        )
    return value


class SlotCreateSerializer(serializers.Serializer):
    """``POST /api/events`` 請求 body 裡單筆候選時段。

    刻意不宣告 ``id`` 欄位——請求若帶了 ``slots[].id``,DRF 只讀取已宣告欄位,
    這個值會被自動忽略,建立時一律由 ``Slot`` model 的 UUID 預設值產生。
    """

    date = serializers.DateField()
    time = serializers.TimeField(required=False, allow_null=True)
    # max_length 對齊 Slot.label 的 varchar(100)——plain Serializer 不會像
    # ModelSerializer 一樣自動從 model 繼承欄位限制,若不宣告,超長 label 要到
    # DB INSERT 才會炸,而不是乾淨的 400。
    label = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, max_length=100
    )


class EventCreateSerializer(serializers.ModelSerializer):
    """``POST /api/events`` 請求 body。

    ``hostEmail`` 刻意不宣告成欄位——即使請求 body 帶了這個 key,DRF 只讀取已宣告
    欄位,不會被採信。``Event.host_email`` 改由 view(``EventCreateView.post``)在呼叫
    ``serializer.save()`` 時額外帶入 ``request.user.email``,一律取自已登入使用者
    自己的帳號 email。
    """

    hostNickname = serializers.CharField(source="host_nickname")
    responseDeadline = serializers.DateTimeField(source="response_deadline")
    slots = SlotCreateSerializer(many=True)

    class Meta:
        model = Event
        fields = [
            "title",
            "hostNickname",
            "mode",
            "slots",
            "responseDeadline",
            "location",
            "description",
        ]

    def validate_hostNickname(self, value):
        return _validate_host_nickname_weighted_length(value)

    def validate_responseDeadline(self, value):
        return _validate_response_deadline_in_future(value)

    def validate_slots(self, value):
        if len(value) < MIN_SLOTS:
            raise serializers.ValidationError(
                f"候選時段至少須有 {MIN_SLOTS} 筆", code="SLOTS_REQUIRED"
            )
        if len(value) > MAX_SLOTS:
            raise serializers.ValidationError(
                f"候選時段最多 {MAX_SLOTS} 個", code="TOO_MANY_SLOTS"
            )
        return value

    def create(self, validated_data):
        slots_data = validated_data.pop("slots")
        owner = validated_data.pop("owner")
        # Event.id 用 secrets 隨機產生,理論上可能撞號(機率極低,見
        # ids.generate_short_id 的說明)。每次呼叫 Event.objects.create() 都會
        # 重新產生一個新 id,撞到就重試;重試次數用完仍撞號就讓 IntegrityError
        # 往上炸,不吞掉。
        for attempt in range(EVENT_ID_COLLISION_MAX_ATTEMPTS):
            try:
                with transaction.atomic():
                    event = Event.objects.create(owner=owner, **validated_data)
                    Slot.objects.bulk_create(
                        [Slot(event=event, **slot_data) for slot_data in slots_data]
                    )
                return event
            except IntegrityError:
                if attempt == EVENT_ID_COLLISION_MAX_ATTEMPTS - 1:
                    raise


class EventPatchSerializer(serializers.ModelSerializer):
    """``PATCH /api/events/{id}`` 請求 body — 已登入擁有者的 partial update。

    六個可改欄位皆 ``required=False``(搭配 view 端傳入 ``partial=True``),請求中
    未包含的欄位保持原值不變。刻意不宣告 ``mode``/``slots``——DRF 只讀取已宣告
    欄位,未宣告的輸入自動被忽略,不需要額外手動剔除。

    ``hostEmail`` 與 ``EventCreateSerializer`` 不同,這裡**必須**宣告成一般可寫
    欄位——PATCH 情境下允許改成與登入帳號 email 不同的任意合法信箱,不再綁定
    ``request.user.email``。
    """

    hostNickname = serializers.CharField(source="host_nickname", required=False)
    hostEmail = serializers.EmailField(source="host_email", required=False)
    responseDeadline = serializers.DateTimeField(source="response_deadline", required=False)

    class Meta:
        model = Event
        fields = [
            "title",
            "description",
            "location",
            "hostNickname",
            "hostEmail",
            "responseDeadline",
        ]
        extra_kwargs = {
            "title": {"required": False},
            "description": {"required": False},
            "location": {"required": False},
        }

    def validate_hostNickname(self, value):
        return _validate_host_nickname_weighted_length(value)

    def validate_responseDeadline(self, value):
        return _validate_response_deadline_in_future(value)


class SlotSerializer(serializers.ModelSerializer):
    """``GET /api/events/{id}`` 回應裡巢狀的候選時段。"""

    class Meta:
        model = Slot
        fields = ["id", "date", "time", "label"]


class _OwnerAndDisplayStatusMixin:
    """``isOwner``/``displayStatus`` 的共用計算邏輯。

    ``EventDetailSerializer``/``EventSummarySerializer`` 都需要這兩個
    ``SerializerMethodField``,計算方式完全相同,抽出來避免兩份序列化器各寫一次。
    """

    def _is_owner(self, event):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        return bool(user) and user.is_authenticated and user == event.owner

    def get_isOwner(self, event):
        return self._is_owner(event)

    def get_displayStatus(self, event):
        # EventDetailView.get() 為了判斷要不要回 410 LINK_EXPIRED,已經算過一次
        # displayStatus,算好的值會放進 context 直接複用,不用重算(同一個純函式、
        # 同一組參數,沒有理由算兩次)。其他呼叫端(例如清單頁)沒有預先算過,
        # 正常呼叫這個純函式即可。
        precomputed = self.context.get("display_status")
        if precomputed is not None:
            return precomputed
        return compute_display_status(
            event.status,
            event.response_deadline,
            event.finalized_at,
            event.cancelled_at,
            event.final_slot.date if event.final_slot else None,
            timezone.now(),
        )


class EventDetailSerializer(_OwnerAndDisplayStatusMixin, serializers.ModelSerializer):
    """``GET /api/events/{id}`` 回應 — 活動完整資料,任何人(含未登入)可查詢。

    ``isOwner``/``hostEmail``/``displayStatus``/``responses`` 都需要拿到目前
    請求者身分或即時計算,透過 view 傳入 ``context={"request": request}`` 讓這幾個
    ``SerializerMethodField`` 使用。
    """

    hostNickname = serializers.CharField(source="host_nickname")
    hostEmail = serializers.SerializerMethodField()
    responseDeadline = serializers.DateTimeField(source="response_deadline")
    displayStatus = serializers.SerializerMethodField()
    isOwner = serializers.SerializerMethodField()
    slots = SlotSerializer(many=True, read_only=True)
    responses = serializers.SerializerMethodField()

    class Meta:
        model = Event
        fields = [
            "id",
            "title",
            "hostNickname",
            "hostEmail",
            "mode",
            "responseDeadline",
            "location",
            "description",
            "status",
            "displayStatus",
            "isOwner",
            "slots",
            "responses",
        ]

    def get_hostEmail(self, event):
        if not self._is_owner(event):
            return None
        return event.host_email

    def get_responses(self, event):
        # ParticipantResponse model 尚未建立,固定回傳空陣列。
        return []


class EventSummarySerializer(_OwnerAndDisplayStatusMixin, serializers.ModelSerializer):
    """``GET /api/events?owner=me`` 回應裡的精簡格式活動清單項目。

    刻意不宣告 ``responses``/``hostEmail`` 欄位——清單頁不含個別參與者投票明細與
    主揪 Email。
    """

    hostNickname = serializers.CharField(source="host_nickname")
    responseDeadline = serializers.DateTimeField(source="response_deadline")
    displayStatus = serializers.SerializerMethodField()
    isOwner = serializers.SerializerMethodField()
    responseCount = serializers.SerializerMethodField()

    class Meta:
        model = Event
        fields = [
            "id",
            "title",
            "hostNickname",
            "mode",
            "responseDeadline",
            "location",
            "description",
            "status",
            "displayStatus",
            "isOwner",
            "responseCount",
        ]

    def get_responseCount(self, event):
        # ParticipantResponse model 尚未建立,固定回傳 0。
        return 0
