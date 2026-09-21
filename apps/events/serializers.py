import re
import unicodedata

from django.contrib.auth.hashers import make_password
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import serializers

from config.exceptions import ApiError

from .lifecycle import compute_display_status
from .models import Event, ParticipantResponse, ParticipantResponseSlotAvailability, Slot

HOST_NICKNAME_MAX_WEIGHTED_LENGTH = 40
MIN_SLOTS = 1
MAX_SLOTS = 20
EVENT_ID_COLLISION_MAX_ATTEMPTS = 3
PARTICIPANT_RESPONSE_ID_COLLISION_MAX_ATTEMPTS = 3

# re.ASCII:\d 預設是 Unicode-aware,會放行全形／阿拉伯數字等非 ASCII 數字字元
# ——這裡刻意收斂成純 ASCII 0-9,否則同一支手機末三碼日後可能用不同輸入法
# 打出兩種「看起來一樣」但雜湊不同的字串,導致合法使用者被鎖在自己的投票外
# (code-review 抓到,見 add-participant-responses 的 code-review 紀錄)。
PHONE_LAST_THREE_RE = re.compile(r"^\d{3}$", re.ASCII)


class SlotAvailabilityInputSerializer(serializers.Serializer):
    """``slotAvailabilities`` 陣列裡單筆表態:``{slotId, availability}``。
    ``ParticipantResponseCreateSerializer``/``ParticipantResponsePatchSerializer``
    共用同一份巢狀格式(見 design.md D4 2026-09-21 修訂)。"""

    slotId = serializers.UUIDField()
    availability = serializers.ChoiceField(
        choices=ParticipantResponseSlotAvailability.Availability.values
    )


def _validate_slot_availabilities(value, event):
    """驗證 ``slotAvailabilities``:每個 ``slotId`` 皆屬於指定活動、且該活動
    全部候選時段都必須恰好出現一次(不可缺漏、不可重複)——已與使用者確認
    每次送出都是該活動候選時段的完整表態,不是部分更新(design.md D4 2026-09-21
    修訂③)。``ParticipantResponseCreateSerializer``/
    ``ParticipantResponsePatchSerializer`` 的 ``validate_slotAvailabilities``
    共用同一份檢查邏輯,避免兩處各寫一次容易不一致。"""
    event_slot_ids = set(event.slots.values_list("id", flat=True))
    submitted_slot_ids = [item["slotId"] for item in value]
    submitted_slot_id_set = set(submitted_slot_ids)

    if not submitted_slot_id_set.issubset(event_slot_ids):
        raise serializers.ValidationError(
            "候選時段不存在於此活動", code="SLOT_NOT_FOUND"
        )
    if (
        submitted_slot_id_set != event_slot_ids
        or len(submitted_slot_ids) != len(event_slot_ids)
    ):
        raise serializers.ValidationError(
            "每個候選時段都必須表態，且不可重複",
            code="SLOT_AVAILABILITY_INCOMPLETE",
        )
    return value


def _validate_phone_last_three_format(value):
    """`phoneLastThree` 3 位數字格式驗證,`ParticipantResponseCreateSerializer`/
    `ParticipantResponseVerifySerializer` 共用。"""
    if not PHONE_LAST_THREE_RE.match(value):
        raise serializers.ValidationError(
            "手機末三碼須為 3 位數字", code="PHONE_LAST_THREE_INVALID"
        )
    return value


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


class ParticipantResponseCreateSerializer(serializers.Serializer):
    """``POST /api/events/{id}/responses`` 請求 body — 參與者初次投票。

    Plain ``Serializer``(不是 ``ModelSerializer``)——``phoneLastThree`` 需要
    先雜湊才能寫入 ``ParticipantResponse.phone_last_three_hash``,
    ``slotAvailabilities`` 對應的是帶額外欄位的 M2M 關聯而非單一 model 欄位,
    兩者都不適合用 ``source=`` 直接映射。View 呼叫時須帶入
    ``context={"event": event}``,``validate_slotAvailabilities`` 用來確認
    候選時段確實屬於該活動、且每個時段都恰好表態一次。
    """

    nickname = serializers.CharField(max_length=40)
    phoneLastThree = serializers.CharField()
    email = serializers.EmailField(required=False, allow_null=True, default=None)
    # comment 是自由文字留言,不像 email 有「格式對不對」的概念——空字串就是
    # 「沒有留言」,沒有理由當成錯誤拒絕,所以刻意 allow_blank=True(跟 email
    # 的 blank 視為無效是不同的決策,見 config/exceptions.py 的對照)。
    comment = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, max_length=200, default=None
    )
    # ``ChildSerializer(many=True)``——實測 DRF 對這種寫法的巢狀驗證錯誤形狀是
    # 「以索引為 key 的 dict」(``{0: {"slotId": [...]}}``),不是原本以為的
    # list。``config/exceptions.py`` 的 ``_build_errors`` 已對照這個真實形狀
    # 展開成 ``slotAvailabilities[0].slotId`` 這種巢狀欄位路徑,沿用
    # ``EventCreateSerializer.slots`` 同一種寫法(見 add-participant-responses
    # 的 code-review 紀錄——這個形狀誤解連帶修正了 slots[] 既有的同款 bug)。
    slotAvailabilities = SlotAvailabilityInputSerializer(many=True)

    def validate_nickname(self, value):
        trimmed = value.strip()
        event = self.context["event"]
        if trimmed == event.host_nickname:
            raise serializers.ValidationError(
                "此暱稱與主揪暱稱相同，請改用其他暱稱",
                code="NICKNAME_CONFLICTS_WITH_HOST",
            )
        return trimmed

    def validate_phoneLastThree(self, value):
        return _validate_phone_last_three_format(value)

    def validate_slotAvailabilities(self, value):
        return _validate_slot_availabilities(value, self.context["event"])

    def create(self, validated_data):
        event = self.context["event"]
        nickname = validated_data["nickname"]
        availabilities = validated_data["slotAvailabilities"]
        phone_last_three_hash = make_password(validated_data["phoneLastThree"])
        comment = validated_data.get("comment") or None

        # ParticipantResponse.id 短 id 碰撞、與 unique_together (event,
        # nickname) 暱稱重複,是同一個 create() 呼叫下兩個獨立來源都可能觸發
        # 的 IntegrityError——捕獲後先查暱稱是否已存在來區分成因,見
        # openspec/changes/add-participant-responses/design.md D9。
        for attempt in range(PARTICIPANT_RESPONSE_ID_COLLISION_MAX_ATTEMPTS):
            try:
                with transaction.atomic():
                    response = ParticipantResponse.objects.create(
                        event=event,
                        nickname=nickname,
                        phone_last_three_hash=phone_last_three_hash,
                        email=validated_data.get("email"),
                        comment=comment,
                    )
                    ParticipantResponseSlotAvailability.objects.bulk_create(
                        [
                            ParticipantResponseSlotAvailability(
                                response=response,
                                slot_id=item["slotId"],
                                availability=item["availability"],
                            )
                            for item in availabilities
                        ]
                    )
                return response
            except IntegrityError:
                if ParticipantResponse.objects.filter(
                    event=event, nickname=nickname
                ).exists():
                    raise ApiError(
                        "此暱稱已被使用，請改用「更新投票」",
                        code="NICKNAME_TAKEN",
                        status_code=400,
                    ) from None
                if attempt == PARTICIPANT_RESPONSE_ID_COLLISION_MAX_ATTEMPTS - 1:
                    raise


class ParticipantResponseVerifySerializer(serializers.Serializer):
    """``POST /api/events/{id}/responses/verify`` 請求 body — 參與者核對身分。

    只做欄位格式驗證(暱稱 trim、手機末三碼格式)——是否真的核對成功(查無此
    暱稱、或暱稱存在但手機末三碼雜湊不符)刻意留給 view 處理並統一回應同一種
    401 `IDENTITY_VERIFICATION_FAILED`(D3),不在 serializer 層拆成兩種錯誤,
    避免回應差異變成「暱稱是否存在」的 side channel。
    """

    nickname = serializers.CharField(max_length=40)
    phoneLastThree = serializers.CharField()

    def validate_nickname(self, value):
        return value.strip()

    def validate_phoneLastThree(self, value):
        return _validate_phone_last_three_format(value)


class ParticipantResponsePatchSerializer(serializers.Serializer):
    """``PATCH /api/events/{id}/responses/{responseId}`` 請求 body — 參與者
    更新投票的候選時段。

    只宣告 ``slotAvailabilities``——``nickname``/``email``/``phoneLastThree`` 刻意
    不宣告,即使請求 body 帶了這些 key,DRF 只讀取已宣告欄位,不會被採信,沿用
    專案既有「未宣告欄位自動被忽略」慣例(見 ``EventPatchSerializer`` 同款寫
    法)。``accessToken`` 也不在這裡宣告——存取憑證的驗證屬於認證/授權範疇
    (比對 token_hash、效期、是否已使用、關聯的 response 是否對得上 URL 的
    ``responseId``),不是『這次要改成什麼』的資料驗證,兩者關注點不同,改由
    view 層(``ParticipantResponseDetailView.patch()``)直接讀 ``request.data``
    處理。View 呼叫時須帶入 ``context={"event": event}``,供
    ``validate_slotAvailabilities`` 確認候選時段確實屬於該活動、且每個時段
    都恰好表態一次(每次更新都是完整覆蓋,見 design.md D4 2026-09-21 修訂③)。
    """

    # 見 ParticipantResponseCreateSerializer 對應欄位的說明——``many=True`` 的
    # 巢狀驗證錯誤形狀是「以索引為 key 的 dict」，被 `_build_errors` 正確展開。
    slotAvailabilities = SlotAvailabilityInputSerializer(many=True)

    def validate_slotAvailabilities(self, value):
        return _validate_slot_availabilities(value, self.context["event"])


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
        # D8:只回傳 nickname/slotAvailabilities,刻意不含 phoneLastThree(含雜湊)
        # /email——那些屬於參與者聯絡資訊,不對外(含其他參與者)公開。三態表態
        # 見 design.md D4/D8(2026-09-21 修訂)。view 端(_event_with_responses_queryset())
        # 已 prefetch_related("responses__slot_availabilities"),這裡用 .all()
        # 走的是 prefetch cache,不會額外觸發 query。
        return [
            {
                "id": participant_response.id,
                "nickname": participant_response.nickname,
                "slotAvailabilities": [
                    {
                        "slotId": str(availability.slot_id),
                        "availability": availability.availability,
                    }
                    for availability in participant_response.slot_availabilities.all()
                ],
            }
            for participant_response in event.responses.all()
        ]


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
