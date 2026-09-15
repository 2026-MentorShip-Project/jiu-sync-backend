import unicodedata

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from .lifecycle import compute_display_status
from .models import Event, Slot

HOST_NICKNAME_MAX_WEIGHTED_LENGTH = 40
MIN_SLOTS = 1
MAX_SLOTS = 20


def _weighted_length(value):
    """中日韓(CJK)字元計 2、其餘字元計 1 的加權長度。

    以 ``unicodedata.east_asian_width`` 判斷:Wide/Fullwidth 視為 CJK 字元。
    見 openspec/changes/add-events-api/specs/events/spec.md「主揪建立活動」。
    """
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in value)


class SlotCreateSerializer(serializers.Serializer):
    """``POST /api/events`` 請求 body 裡單筆候選時段。

    刻意不宣告 ``id`` 欄位——請求若帶了 ``slots[].id``,DRF 只讀取已宣告欄位,
    這個值會被自動忽略,建立時一律由 ``Slot`` model 的 UUID 預設值產生。見 design.md D1。
    """

    date = serializers.DateField()
    time = serializers.TimeField(required=False, allow_null=True)
    # max_length 對齊 Slot.label 的 varchar(100)——這是 plain Serializer,不會像
    # ModelSerializer 一樣自動從 model 繼承欄位限制,若不宣告,超長 label 會通過
    # 驗證、直到 INSERT 才被 DB 拋 DataError(500),而不是乾淨的 400。見 Codex
    # review 修正項目 B。
    label = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, max_length=100
    )


class EventCreateSerializer(serializers.ModelSerializer):
    """``POST /api/events`` 請求 body。

    ``hostEmail`` 刻意不宣告成欄位——即使請求 body 帶了這個 key,DRF 只讀取已宣告
    欄位,不會被採信、也不會寫進 ``Event.host_email``。見 spec「主揪建立活動」與
    design.md D7(camelCase 用 ``source=`` 手動映射)。
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
        if _weighted_length(value) > HOST_NICKNAME_MAX_WEIGHTED_LENGTH:
            raise serializers.ValidationError(
                f"主揪暱稱加權長度(CJK 字元計 2、其餘計 1)不得超過"
                f" {HOST_NICKNAME_MAX_WEIGHTED_LENGTH}"
            )
        return value

    def validate_responseDeadline(self, value):
        if value <= timezone.now():
            raise serializers.ValidationError("投票截止時間必須晚於目前時間")
        return value

    def validate_slots(self, value):
        if not (MIN_SLOTS <= len(value) <= MAX_SLOTS):
            raise serializers.ValidationError(
                f"候選時段筆數須介於 {MIN_SLOTS} 至 {MAX_SLOTS} 之間"
            )
        return value

    def create(self, validated_data):
        slots_data = validated_data.pop("slots")
        owner = validated_data.pop("owner")
        with transaction.atomic():
            event = Event.objects.create(owner=owner, **validated_data)
            Slot.objects.bulk_create(
                [Slot(event=event, **slot_data) for slot_data in slots_data]
            )
        return event


class SlotSerializer(serializers.ModelSerializer):
    """``GET /api/events/{id}`` 回應裡巢狀的候選時段。"""

    class Meta:
        model = Slot
        fields = ["id", "date", "time", "label"]


class _OwnerAndDisplayStatusMixin:
    """``isOwner``/``displayStatus`` 的共用計算邏輯。

    ``EventDetailSerializer``(``GET /api/events/{id}``)與 ``EventSummarySerializer``
    (``GET /api/events?owner=me``)都需要這兩個 ``SerializerMethodField``,計算方式
    完全相同(比對 ``request.user`` 與 ``event.owner``、呼叫
    ``lifecycle.compute_display_status``),抽出來避免兩份序列化器各寫一次。見
    design.md D3(displayStatus 用純函式)、D4(owner 比對放在 serializer)。
    """

    def _is_owner(self, event):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        return bool(user) and user.is_authenticated and user == event.owner

    def get_isOwner(self, event):
        return self._is_owner(event)

    def get_displayStatus(self, event):
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
    ``SerializerMethodField`` 使用。見 design.md D3(displayStatus 用
    ``lifecycle.compute_display_status``)、D4(email 遮罩邏輯放在 serializer)、
    D6(``responses`` 這次固定回傳 ``[]``)。
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
        # ParticipantResponse 本次不建立(scope 排除項目)——見 design.md D6。
        return []


class EventSummarySerializer(_OwnerAndDisplayStatusMixin, serializers.ModelSerializer):
    """``GET /api/events?owner=me`` 回應裡的精簡格式活動清單項目。

    刻意不宣告 ``responses``/``hostEmail`` 欄位——清單頁不含個別參與者投票明細與
    主揪 Email,見 spec「主揪查詢自己擁有的活動清單」。``responseCount`` 固定回傳
    ``0``(``ParticipantResponse`` 本次未建立,見 design.md D6)。``isOwner``/
    ``displayStatus`` 沿用 ``EventDetailSerializer`` 的計算方式(見
    ``_OwnerAndDisplayStatusMixin``)。
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
        # ParticipantResponse 本次不建立(scope 排除項目)——見 design.md D6。
        return 0
