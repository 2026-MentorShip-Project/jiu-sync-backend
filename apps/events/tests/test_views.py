import hashlib
import re
import secrets
import time
import uuid
from datetime import timedelta

import pytest
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import IntegrityError, connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import dateparse, timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User
from apps.events.ids import generate_short_id
from apps.events.models import (
    Comment,
    Event,
    ParticipantResponse,
    ParticipantResponseAccessToken,
    ParticipantResponseSlotAvailability,
    Slot,
)

pytestmark = pytest.mark.django_db

EVENTS_URL = "/api/events/"
RESPONSE_SHORT_ID_RE = re.compile(r"^[0-9A-Za-z]{8}$")

# Event.id 是 8 碼 base62 短 id,不是 UUID 形狀。
SHORT_ID_RE = re.compile(r"^[0-9A-Za-z]{8}$")


def _detail_url(event_id):
    return f"/api/events/{event_id}/"


def _create_user(email="host@example.com", google_sub="sub-1"):
    return User.objects.create_user(
        email=email, google_sub=google_sub, display_name="Host", avatar_url=""
    )


def _auth_client(user):
    access_token = str(RefreshToken.for_user(user).access_token)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")
    return client


def _valid_payload(**overrides):
    now = timezone.now()
    payload = {
        "title": "颱風天續攤 晚餐",
        "hostNickname": "小明",
        "mode": "date_only",
        "slots": [{"date": "2026-10-01"}],
        "responseDeadline": (now + timedelta(days=3)).isoformat(),
        "location": "台北車站",
        "description": "順便討論下次活動",
    }
    payload.update(overrides)
    return payload


def test_authenticated_user_can_create_event_and_receives_id_and_share_url():
    """① 已登入使用者送出合法欄位 → 201,body 只有 id/shareUrl,DB 多一筆
    Event(owner 為該使用者、status active)與對應 Slot。"""
    user = _create_user()
    client = _auth_client(user)
    assert Event.objects.count() == 0

    response = client.post(EVENTS_URL, _valid_payload(), format="json")

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert set(body.keys()) == {"id", "shareUrl"}
    assert SHORT_ID_RE.match(body["id"])
    assert body["shareUrl"].startswith(settings.FRONTEND_BASE_URL)
    assert body["shareUrl"] == f"{settings.FRONTEND_BASE_URL}/events/{body['id']}"

    assert Event.objects.count() == 1
    event = Event.objects.get()
    assert str(event.id) == body["id"]
    assert event.owner_id == user.id
    assert event.status == "active"
    assert event.slots.count() == 1


def test_unauthenticated_user_cannot_create_event():
    """② 未登入(不帶 token)→ 401,不建立任何資料。"""
    client = APIClient()
    assert Event.objects.count() == 0

    response = client.post(EVENTS_URL, _valid_payload(), format="json")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert Event.objects.count() == 0


def test_response_deadline_equal_to_or_earlier_than_now_returns_400():
    """③ responseDeadline 等於或早於送出當下時間 → 400,code 為 "DEADLINE_IN_PAST",
    不建立任何資料。

    新增 code 斷言:後續 change 追加的刻意行為變更(400 驗證錯誤改回每條規則配
    專屬 code,見 add-error-code-table 的修訂記錄),不是遷就實作結果。
    """
    user = _create_user()
    client = _auth_client(user)
    now = timezone.now()

    for deadline in (now, now - timedelta(hours=1)):
        response = client.post(
            EVENTS_URL, _valid_payload(responseDeadline=deadline.isoformat()), format="json"
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["code"] == "DEADLINE_IN_PAST"

    assert Event.objects.count() == 0


def test_slots_count_zero_or_over_20_returns_400():
    """④ slots 0 筆 → 400,code 為 "SLOTS_REQUIRED";超過 20 筆 → 400,code 為
    "TOO_MANY_SLOTS"。不建立任何資料。

    新增 code 斷言,理由同上(add-error-code-table 修訂記錄)。
    """
    user = _create_user()
    client = _auth_client(user)
    too_many_slots = [{"date": f"2026-10-{day:02d}"} for day in range(1, 22)]

    response = client.post(EVENTS_URL, _valid_payload(slots=[]), format="json")
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "SLOTS_REQUIRED"

    response = client.post(EVENTS_URL, _valid_payload(slots=too_many_slots), format="json")
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "TOO_MANY_SLOTS"

    assert Event.objects.count() == 0


def test_title_over_30_chars_returns_400():
    """⑤ title 超過 30 字元 → 400,code 為 "TITLE_TOO_LONG"。

    新增 code 斷言,理由同上(add-error-code-table 修訂記錄)。
    """
    user = _create_user()
    client = _auth_client(user)

    response = client.post(EVENTS_URL, _valid_payload(title="揪" * 31), format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "TITLE_TOO_LONG"
    assert Event.objects.count() == 0


def test_field_code_overrides_for_required_and_invalid_choice_and_max_length():
    """新增:補齊 FIELD_CODE_OVERRIDES 剩下沒被其他測試覆蓋到的對照表項目——
    title/hostNickname 未填(required)、mode 不合法(invalid_choice)、
    location/description 超過長度上限(max_length),各自的 code 是否真的對得上
    對照表(而不是漏配、退回 DRF 原始的 "required"/"max_length" 等泛用字串)。"""
    user = _create_user()
    client = _auth_client(user)

    payload_without_title = _valid_payload()
    del payload_without_title["title"]
    response = client.post(EVENTS_URL, payload_without_title, format="json")
    assert response.json()["code"] == "TITLE_REQUIRED"

    payload_without_nickname = _valid_payload()
    del payload_without_nickname["hostNickname"]
    response = client.post(EVENTS_URL, payload_without_nickname, format="json")
    assert response.json()["code"] == "HOST_NICKNAME_REQUIRED"

    response = client.post(EVENTS_URL, _valid_payload(mode="not-a-real-mode"), format="json")
    assert response.json()["code"] == "MODE_INVALID"

    response = client.post(EVENTS_URL, _valid_payload(location="l" * 201), format="json")
    assert response.json()["code"] == "LOCATION_TOO_LONG"

    response = client.post(EVENTS_URL, _valid_payload(description="d" * 51), format="json")
    assert response.json()["code"] == "DESCRIPTION_TOO_LONG"

    assert Event.objects.count() == 0


def test_host_nickname_weighted_length_exactly_40_is_allowed():
    """⑥ 邊界:hostNickname 加權長度剛好等於 40(CJK 字元計 2)→ 通過。"""
    user = _create_user()
    client = _auth_client(user)
    nickname = "揪" * 20  # 20 CJK chars * weight 2 = 40

    response = client.post(EVENTS_URL, _valid_payload(hostNickname=nickname), format="json")

    assert response.status_code == status.HTTP_201_CREATED


def test_host_nickname_weighted_length_over_40_returns_400():
    """⑥ hostNickname 加權長度超過 40(CJK 字元計 2、其餘計 1)→ 400,code 為
    "HOST_NICKNAME_TOO_LONG"。"""
    user = _create_user()
    client = _auth_client(user)
    nickname = "揪" * 21  # 21 CJK chars * weight 2 = 42

    response = client.post(EVENTS_URL, _valid_payload(hostNickname=nickname), format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "HOST_NICKNAME_TOO_LONG"
    assert Event.objects.count() == 0


def test_slot_id_in_request_is_ignored_and_system_generates_uuid():
    """⑦ 請求 body 的 slots[].id 帶入任意字串 → 建立成功後,DB 裡 Slot 的實際 id
    是系統產生的 UUID,不等於請求傳入的值。"""
    user = _create_user()
    client = _auth_client(user)
    payload = _valid_payload(slots=[{"date": "2026-10-01", "id": "not-a-real-uuid"}])

    response = client.post(EVENTS_URL, payload, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    event = Event.objects.get()
    slot = event.slots.get()
    assert str(slot.id) != "not-a-real-uuid"
    # 不會拋例外代表是合法 UUID。
    uuid.UUID(str(slot.id))


def test_host_email_in_request_body_is_ignored_and_set_from_authenticated_user():
    """⑧ 請求 body 帶入 hostEmail → 不採信該欄位,建立後 Event.host_email 一律為
    已登入使用者自己的帳號 email(建立當下即自動代入,不必等待未來的 PATCH)。"""
    user = _create_user(email="real-owner@example.com")
    client = _auth_client(user)
    payload = _valid_payload(hostEmail="ignored@example.com")

    response = client.post(EVENTS_URL, payload, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    event = Event.objects.get()
    assert event.host_email == "real-owner@example.com"
    assert event.host_email != "ignored@example.com"


def test_host_email_is_set_from_authenticated_user_even_without_request_body_field():
    """⑧-2 請求 body 完全不帶 hostEmail → Event.host_email 仍自動代入已登入使用者的
    帳號 email,不維持 None。"""
    user = _create_user(email="another-owner@example.com")
    client = _auth_client(user)
    payload = _valid_payload()
    assert "hostEmail" not in payload

    response = client.post(EVENTS_URL, payload, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    event = Event.objects.get()
    assert event.host_email == "another-owner@example.com"


def test_slot_label_exactly_100_chars_is_allowed():
    """⑨ 邊界:slot label 剛好 100 字元(DB 欄位 varchar(100)上限)→ 201 成功。"""
    user = _create_user()
    client = _auth_client(user)
    label = "a" * 100
    payload = _valid_payload(slots=[{"date": "2026-10-01", "label": label}])

    response = client.post(EVENTS_URL, payload, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    event = Event.objects.get()
    assert event.slots.get().label == label


def test_slot_label_over_100_chars_returns_400():
    """⑨ slot label 超過 100 字元 → 400 驗證錯誤(不是 DB DataError 500),不建立任何資料。"""
    user = _create_user()
    client = _auth_client(user)
    payload = _valid_payload(slots=[{"date": "2026-10-01", "label": "a" * 101}])

    response = client.post(EVENTS_URL, payload, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert Event.objects.count() == 0


def test_slot_label_over_100_chars_error_has_nested_semantic_code():
    """code-review 補充:確認上一則測試的 400 回應,`errors[]` 裡巢狀欄位
    `slots[0].label` 的 code 真的是語意化的 SLOT_LABEL_TOO_LONG,不是未對照的
    DRF 原始碼——`config/exceptions.py::_build_errors` 原本誤判
    `ChildSerializer(many=True)` 的巢狀驗證錯誤形狀是 list,實際是以索引為
    key 的 dict,這條巢狀 code 對照從未真的生效過(add-participant-responses
    的 code-review 修正)。"""
    user = _create_user()
    client = _auth_client(user)
    payload = _valid_payload(slots=[{"date": "2026-10-01", "label": "a" * 101}])

    response = client.post(EVENTS_URL, payload, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    body = response.json()
    matching = [e for e in body["errors"] if e["field"] == "slots[0].label"]
    assert len(matching) == 1
    assert matching[0]["code"] == "SLOT_LABEL_TOO_LONG"


@override_settings(FRONTEND_BASE_URL="https://example.com/")
def test_share_url_has_no_double_slash_when_frontend_base_url_has_trailing_slash():
    """⑩ FRONTEND_BASE_URL 帶結尾斜線(例如 "https://example.com/")→ 組出的
    shareUrl 不應出現雙斜線。"""
    user = _create_user()
    client = _auth_client(user)

    response = client.post(EVENTS_URL, _valid_payload(), format="json")

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert "//events/" not in body["shareUrl"].removeprefix("https://")
    assert body["shareUrl"] == f"https://example.com/events/{body['id']}"


def _create_event(owner, **overrides):
    now = timezone.now()
    defaults = {
        "owner": owner,
        "title": "颱風天續攤 晚餐",
        "host_nickname": "小明",
        "mode": "date_only",
        "response_deadline": now + timedelta(days=3),
        "location": "台北車站",
        "description": "順便討論下次活動",
    }
    defaults.update(overrides)
    event = Event.objects.create(**defaults)
    Slot.objects.create(event=event, date="2026-10-01")
    return event


def _patch_event_id_default(monkeypatch, fake):
    """設定 ``Event.id`` 欄位的 ``default``。

    Django 把解析後的 default getter 快取在 ``Field._get_default``
    (``cached_property``)——一旦任何一筆 ``Event`` 被建立過,這個快取就定型了,
    之後單改 ``field.default`` 不會生效,必須連快取一起清掉才能讓新的
    default 真正生效。
    """
    field = Event._meta.get_field("id")
    monkeypatch.setattr(field, "default", fake)
    monkeypatch.delitem(field.__dict__, "_get_default", raising=False)


def test_event_id_collision_retries_and_still_succeeds(monkeypatch):
    """⑪ Event.id 產生器撞到既有 id 時重試,換到不重複的 id 後仍建立成功。"""
    user = _create_user()
    client = _auth_client(user)
    existing = _create_event(user)
    real_generate = generate_short_id
    calls = {"n": 0}

    def colliding_once_then_real():
        calls["n"] += 1
        return existing.id if calls["n"] == 1 else real_generate()

    _patch_event_id_default(monkeypatch, colliding_once_then_real)

    response = client.post(EVENTS_URL, _valid_payload(), format="json")

    assert response.status_code == status.HTTP_201_CREATED
    assert calls["n"] >= 2
    assert Event.objects.count() == 2


def test_event_id_collision_exhausts_retries_and_raises(monkeypatch):
    """⑫ 每次產生的 id 都撞號 → 重試用盡後仍然失敗(不是無限重試),不留下新資料。"""
    user = _create_user()
    client = _auth_client(user)
    existing = _create_event(user)
    _patch_event_id_default(monkeypatch, lambda: existing.id)

    with pytest.raises(IntegrityError):
        client.post(EVENTS_URL, _valid_payload(), format="json")

    assert Event.objects.count() == 1


def test_unauthenticated_user_can_view_event_detail_without_owner_info():
    """① 未登入請求已存在的活動 → 200,isOwner=false,hostEmail=null。"""
    owner = _create_user()
    event = _create_event(owner, host_email="host@example.com")
    client = APIClient()

    response = client.get(_detail_url(event.id))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["isOwner"] is False
    assert body["hostEmail"] is None


def test_owner_can_view_event_detail_with_actual_host_email():
    """② 活動擁有者本人請求 → 200,isOwner=true,hostEmail 為實際填寫的值。"""
    owner = _create_user()
    event = _create_event(owner, host_email="host@example.com")
    client = _auth_client(owner)

    response = client.get(_detail_url(event.id))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["isOwner"] is True
    assert body["hostEmail"] == "host@example.com"


def test_owner_can_view_event_detail_with_null_host_email_when_unset():
    """② 若擁有者尚未填寫 host_email(None)→ hostEmail 仍回傳 null,isOwner 仍為 true。"""
    owner = _create_user()
    event = _create_event(owner, host_email=None)
    client = _auth_client(owner)

    response = client.get(_detail_url(event.id))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["isOwner"] is True
    assert body["hostEmail"] is None


def test_non_owner_authenticated_user_cannot_see_host_email():
    """③ 其他已登入(非擁有者)使用者請求 → 200,isOwner=false,hostEmail=null。"""
    owner = _create_user(email="host@example.com", google_sub="sub-1")
    other_user = _create_user(email="other@example.com", google_sub="sub-2")
    event = _create_event(owner, host_email="host@example.com")
    client = _auth_client(other_user)

    response = client.get(_detail_url(event.id))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["isOwner"] is False
    assert body["hostEmail"] is None


def test_nonexistent_event_id_returns_404_with_event_not_found_code():
    """④ 請求不存在的 id → 404,body 符合 api-error-format 的 {message, code} 形狀,
    code 為活動專屬的 "EVENT_NOT_FOUND"(不是泛用的 "NOT_FOUND")。

    刻意用「形狀合法、但沒有對應資料」的短 id(而非隨機格式錯誤的字串)——這
    樣不管 URL 路由層用的是 `<str:id>` 還是自訂的 8 碼 base62 converter,都能
    確保請求真的會走到 view 層(`custom_exception_handler` 包裝的
    {message, code} 形狀),而不是路由層級比對不到路徑的 404(那個不會經過同一
    個 exception handler)。

    code 從泛用 "NOT_FOUND" 改成專屬 "EVENT_NOT_FOUND":這是後續 change 追加的
    刻意行為變更(前端要求活動查無資料要有專屬 code,見 add-error-code-table
    的修訂記錄),不是遷就實作結果而放寬測試。
    """
    client = APIClient()

    response = client.get(_detail_url(generate_short_id()))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    body = response.json()
    assert set(body.keys()) == {"message", "code"}
    assert body["code"] == "EVENT_NOT_FOUND"


def test_event_detail_link_expired_returns_410_with_link_expired_code():
    """新增:displayStatus 算出 "link_expired" 時(活動已定案/取消超過 7 天)→
    410 Gone,不是 200,code 為 "LINK_EXPIRED"。

    目前系統沒有 finalize/cancel 端點,無法透過任何 API 真的把活動變成這個
    狀態,直接用 Event.objects.create(..., status=..., cancelled_at=...) 建立
    測試資料驗證,不透過任何 API。
    """
    owner = _create_user()
    event = _create_event(
        owner,
        status=Event.Status.CANCELLED,
        cancelled_at=timezone.now() - timedelta(days=8),
    )
    client = APIClient()

    response = client.get(_detail_url(event.id))

    assert response.status_code == status.HTTP_410_GONE
    body = response.json()
    assert body["code"] == "LINK_EXPIRED"


def test_event_detail_display_status_reflects_expired_deadline():
    """⑤ 回應含 displayStatus,用已過期的 responseDeadline 驗證回傳 voting_closed_pending。"""
    owner = _create_user()
    event = _create_event(owner, response_deadline=timezone.now() - timedelta(hours=1))
    client = APIClient()

    response = client.get(_detail_url(event.id))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["displayStatus"] == "voting_closed_pending"


def test_invalid_bearer_token_can_still_view_event_detail_anonymously():
    """⑦ 帶了格式不正確/無法驗證的 Bearer token → 仍視為匿名請求,200(不是 401),
    isOwner=false,hostEmail=null。"""
    owner = _create_user()
    event = _create_event(owner, host_email="host@example.com")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION="Bearer garbage-invalid-token")

    response = client.get(_detail_url(event.id))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["isOwner"] is False
    assert body["hostEmail"] is None


def test_expired_bearer_token_can_still_view_event_detail_anonymously():
    """⑧ 帶了語法合法但已過期的 Bearer token → 仍視為匿名請求,200(不是 401),
    isOwner=false。同項目 A。"""
    owner = _create_user()
    event = _create_event(owner, host_email="host@example.com")
    client = APIClient()
    expired_token = RefreshToken.for_user(owner).access_token
    expired_token.set_exp(
        from_time=timezone.now() - timedelta(days=1), lifetime=timedelta(seconds=1)
    )
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {expired_token}")

    response = client.get(_detail_url(event.id))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["isOwner"] is False
    assert body["hostEmail"] is None


def test_event_detail_responses_field_is_empty_list_when_no_votes():
    """② 活動無任何投票 → responses 為空陣列(既有行為維持),slotSummary 每個
    時段三態皆為 0(新增,design.md D17)。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    client = APIClient()

    response = client.get(_detail_url(event.id))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["responses"] == []
    assert body["slotSummary"] == [
        {"slotId": str(slot.id), "available": 0, "if_needed": 0, "unavailable": 0}
    ]


def _create_participant_response(event, nickname, availabilities, **overrides):
    """建立一筆 ``ParticipantResponse`` 及其時段表態,直接走 ORM(不經過
    serializer,不受「必須表態該活動全部候選時段」的 API 層驗證限制,見
    design.md D4 2026-09-21 修訂)。

    ``availabilities``:
    - dict ``{slot: "available"/"if_needed"/"unavailable", ...}``——明確指定
      每個時段的表態,測試需要驗證三態細節時用這個形式。
    - ``Slot`` 物件的可迭代(list/tuple)——簡化寫法,每個時段都視為
      ``"available"``,多數測試只關心「這位參與者有標記這個時段」,不需要
      三態細節。
    """
    defaults = {
        "event": event,
        "nickname": nickname,
        "phone_last_three_hash": make_password("123"),
        "email": None,
    }
    defaults.update(overrides)
    participant_response = ParticipantResponse.objects.create(**defaults)
    if isinstance(availabilities, dict):
        items = list(availabilities.items())
    else:
        items = [
            (slot, ParticipantResponseSlotAvailability.Availability.AVAILABLE)
            for slot in availabilities
        ]
    ParticipantResponseSlotAvailability.objects.bulk_create(
        [
            ParticipantResponseSlotAvailability(
                response=participant_response, slot=slot, availability=availability
            )
            for slot, availability in items
        ]
    )
    return participant_response


def test_event_detail_responses_field_contains_real_votes():
    """① 活動有 2 筆投票 → responses 陣列含 2 筆,各自 nickname/slotAvailabilities
    正確(三態)。"""
    owner = _create_user()
    event = _create_event(owner)
    slot_1 = event.slots.first()
    slot_2 = Slot.objects.create(event=event, date="2026-10-02")
    response_1 = _create_participant_response(
        event,
        "小華",
        {slot_1: "available", slot_2: "if_needed"},
        comment="19:00才能到",
    )
    response_2 = _create_participant_response(
        event, "小美", {slot_1: "unavailable", slot_2: "available"}
    )
    client = APIClient()

    response = client.get(_detail_url(event.id))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert len(body["responses"]) == 2
    by_id = {item["id"]: item for item in body["responses"]}
    assert set(by_id.keys()) == {response_1.id, response_2.id}
    assert by_id[response_1.id]["nickname"] == "小華"
    assert by_id[response_1.id]["comment"] == "19:00才能到"
    assert {
        (item["slotId"], item["availability"])
        for item in by_id[response_1.id]["slotAvailabilities"]
    } == {(str(slot_1.id), "available"), (str(slot_2.id), "if_needed")}
    assert by_id[response_2.id]["nickname"] == "小美"
    assert by_id[response_2.id]["comment"] is None
    assert {
        (item["slotId"], item["availability"])
        for item in by_id[response_2.id]["slotAvailabilities"]
    } == {(str(slot_1.id), "unavailable"), (str(slot_2.id), "available")}

    # slotSummary（design.md D17）：slot_1 一票 available（小華）、一票
    # unavailable（小美）；slot_2 一票 if_needed（小華）、一票 available（小美）。
    summary_by_slot_id = {item["slotId"]: item for item in body["slotSummary"]}
    assert summary_by_slot_id[str(slot_1.id)] == {
        "slotId": str(slot_1.id),
        "available": 1,
        "if_needed": 0,
        "unavailable": 1,
    }
    assert summary_by_slot_id[str(slot_2.id)] == {
        "slotId": str(slot_2.id),
        "available": 1,
        "if_needed": 1,
        "unavailable": 0,
    }


def test_event_detail_responses_field_does_not_leak_phone_or_email():
    """③ 確認回應不含 phoneLastThree/email 欄位。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    _create_participant_response(event, "小華", [slot], email="voter@example.com")
    client = APIClient()

    response = client.get(_detail_url(event.id))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert len(body["responses"]) == 1
    item = body["responses"][0]
    assert set(item.keys()) == {"id", "nickname", "comment", "slotAvailabilities"}
    assert "phoneLastThree" not in item
    assert "email" not in item


def test_owner_list_only_contains_own_events_including_cancelled():
    """① 已登入使用者查詢,建立 A 兩筆活動(其中一筆 cancelled)、B 一筆活動,
    用 A 的 token 查詢 → 回應只含 A 的兩筆活動(含已取消那筆),不含 B 的。"""
    user_a = _create_user(email="a@example.com", google_sub="sub-a")
    user_b = _create_user(email="b@example.com", google_sub="sub-b")
    event_a1 = _create_event(user_a)
    event_a2 = _create_event(user_a, status="cancelled", cancelled_at=timezone.now())
    _create_event(user_b)
    client = _auth_client(user_a)

    response = client.get(EVENTS_URL, {"owner": "me"})

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    returned_ids = {item["id"] for item in body}
    assert returned_ids == {str(event_a1.id), str(event_a2.id)}


def test_owner_list_items_are_summary_format_without_responses_or_host_email():
    """② 每筆回應為精簡格式:不含 responses/hostEmail 欄位,含 responseCount。"""
    owner = _create_user()
    _create_event(owner, host_email="host@example.com")
    client = _auth_client(owner)

    response = client.get(EVENTS_URL, {"owner": "me"})

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert len(body) == 1
    item = body[0]
    assert "responses" not in item
    assert "hostEmail" not in item
    assert item["responseCount"] == 0


def test_unauthenticated_user_cannot_list_own_events():
    """③ 未登入(不帶 token)查詢 → 401。"""
    client = APIClient()

    response = client.get(EVENTS_URL, {"owner": "me"})

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_missing_owner_me_query_param_returns_400():
    """④ 缺少 owner=me 查詢參數 → 400 驗證錯誤,code 為 "OWNER_PARAM_REQUIRED"
    (專案慣例:走 serializers.ValidationError,由 custom_exception_handler
    統一包裝)。"""
    user = _create_user()
    client = _auth_client(user)

    response = client.get(EVENTS_URL)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "OWNER_PARAM_REQUIRED"


def _patch_payload(**overrides):
    now = timezone.now()
    payload = {
        "title": "改過的標題",
        "description": "改過的說明",
        "location": "改過的地點",
        "hostNickname": "改過的暱稱",
        "hostEmail": "changed@example.com",
        "responseDeadline": (now + timedelta(days=5)).isoformat(),
    }
    payload.update(overrides)
    return payload


def test_owner_patch_single_field_updates_only_that_field():
    """① 擁有者僅送單一欄位(title)→ 200,該欄位更新、其餘五欄位維持原值,
    回應格式與 GET 詳情相同(含 slots/displayStatus/isOwner)。"""
    owner = _create_user()
    event = _create_event(owner, host_email="host@example.com")
    client = _auth_client(owner)

    response = client.patch(_detail_url(event.id), {"title": "新標題"}, format="json")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["title"] == "新標題"
    assert body["description"] == "順便討論下次活動"
    assert body["location"] == "台北車站"
    assert body["hostNickname"] == "小明"
    assert body["hostEmail"] == "host@example.com"
    assert "slots" in body
    assert "displayStatus" in body
    assert body["isOwner"] is True

    event.refresh_from_db()
    assert event.title == "新標題"
    assert event.description == "順便討論下次活動"
    assert event.location == "台北車站"
    assert event.host_nickname == "小明"
    assert event.host_email == "host@example.com"


def test_owner_patch_all_six_fields_updates_all():
    """② 擁有者一次送六個欄位全部更新 → 200,六欄位皆正確更新並反映在回應中。"""
    owner = _create_user()
    event = _create_event(owner, host_email="host@example.com")
    client = _auth_client(owner)
    payload = _patch_payload()

    response = client.patch(_detail_url(event.id), payload, format="json")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["title"] == payload["title"]
    assert body["description"] == payload["description"]
    assert body["location"] == payload["location"]
    assert body["hostNickname"] == payload["hostNickname"]
    assert body["hostEmail"] == payload["hostEmail"]

    event.refresh_from_db()
    assert event.title == payload["title"]
    assert event.description == payload["description"]
    assert event.location == payload["location"]
    assert event.host_nickname == payload["hostNickname"]
    assert event.host_email == payload["hostEmail"]


def test_owner_patch_empty_body_changes_nothing():
    """③ 空 body {} → 200,不更動任何欄位,回傳目前狀態。"""
    owner = _create_user()
    event = _create_event(owner, host_email="host@example.com")
    client = _auth_client(owner)

    response = client.patch(_detail_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["title"] == event.title
    assert body["description"] == event.description
    assert body["location"] == event.location
    assert body["hostNickname"] == event.host_nickname
    assert body["hostEmail"] == event.host_email

    event.refresh_from_db()
    assert event.title == "颱風天續攤 晚餐"
    assert event.host_email == "host@example.com"


def test_owner_patch_ignores_fields_outside_the_six():
    """④ body 帶 mode(六欄位外的欄位)→ 200,Event.mode 不受影響,忽略該欄位。"""
    owner = _create_user()
    event = _create_event(owner)
    client = _auth_client(owner)
    original_mode = event.mode

    response = client.patch(
        _detail_url(event.id), {"mode": "time_slots"}, format="json"
    )

    assert response.status_code == status.HTTP_200_OK
    event.refresh_from_db()
    assert event.mode == original_mode


def test_non_owner_authenticated_user_cannot_patch_event():
    """⑤ 已登入但非擁有者送出編輯 → 403,code 為通用的 "FORBIDDEN"(刻意不配專屬
    code,見 grill-me 決策:PATCH 非擁有者這類新情況一律用通用 code,不像
    apps.accounts 既有的 REFRESH_TOKEN_NOT_YOURS 那樣配專屬字串),資料庫該筆
    活動完全未變動。"""
    owner = _create_user(email="host@example.com", google_sub="sub-1")
    other_user = _create_user(email="other@example.com", google_sub="sub-2")
    event = _create_event(owner)
    client = _auth_client(other_user)
    original_title = event.title

    response = client.patch(_detail_url(event.id), {"title": "偷改標題"}, format="json")

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["code"] == "FORBIDDEN"
    event.refresh_from_db()
    assert event.title == original_title


def test_unauthenticated_user_cannot_patch_event():
    """⑥ 未登入(不帶 token)→ 401,資料庫未變動。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()
    original_title = event.title

    response = client.patch(_detail_url(event.id), {"title": "偷改標題"}, format="json")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    event.refresh_from_db()
    assert event.title == original_title


def test_patch_finalized_or_cancelled_event_returns_409_with_event_not_active_code():
    """⑦ status="finalized"/"cancelled" 的活動編輯 → 409(不是 400),code 為
    "EVENT_NOT_ACTIVE",資料庫未變動。直接用 Event.objects.create(..., status=...)
    建立測試資料,不透過任何 API。

    狀態碼從 400 改成 409、並加上專屬 code:這是後續 change 追加的刻意行為變更
    (前端明確要求活動狀態衝突用 409 Conflict,見 add-error-code-table 的修訂
    記錄),不是遷就實作結果而放寬測試。
    """
    owner = _create_user()
    client = _auth_client(owner)

    for event_status in (Event.Status.FINALIZED, Event.Status.CANCELLED):
        event = _create_event(owner, status=event_status)
        original_title = event.title

        response = client.patch(
            _detail_url(event.id), {"title": "偷改標題"}, format="json"
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["code"] == "EVENT_NOT_ACTIVE"
        event.refresh_from_db()
        assert event.title == original_title


def test_patch_active_event_with_expired_deadline_still_allows_edit():
    """⑦b status="active" 但 responseDeadline 已過(displayStatus 為
    "voting_closed_pending")時,PATCH 仍允許編輯,不額外擋。這是刻意決策
    (見 add-event-patch/design.md D6 的 2026-09-21 修訂記錄),不是漏洞:
    主揪可能想在投票截止後延長 responseDeadline 重開投票、或修正內容。跟
    參與者端點的 VOTING_CLOSED 檢查刻意不對稱。
    """
    owner = _create_user()
    client = _auth_client(owner)
    event = _create_event(
        owner, response_deadline=timezone.now() - timedelta(days=1)
    )

    response = client.patch(
        _detail_url(event.id), {"title": "投票已截止後仍可編輯"}, format="json"
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["displayStatus"] == "voting_closed_pending"
    event.refresh_from_db()
    assert event.title == "投票已截止後仍可編輯"


def test_patch_response_deadline_equal_to_or_earlier_than_now_returns_400():
    """⑧ responseDeadline 等於或早於送出當下時間 → 400,code 為 "DEADLINE_IN_PAST",
    資料庫未變動。"""
    owner = _create_user()
    event = _create_event(owner)
    client = _auth_client(owner)
    original_deadline = event.response_deadline
    now = timezone.now()

    for deadline in (now, now - timedelta(hours=1)):
        response = client.patch(
            _detail_url(event.id),
            {"responseDeadline": deadline.isoformat()},
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["code"] == "DEADLINE_IN_PAST"

    event.refresh_from_db()
    assert event.response_deadline == original_deadline


def test_patch_host_email_invalid_format_returns_400_with_host_email_invalid_code():
    """新增:hostEmail 格式不合法 → 400,code 為 "HOST_EMAIL_INVALID"(依
    config/exceptions.py 的 FIELD_CODE_OVERRIDES 把 DRF EmailField 原始的
    "invalid" code 換成語意化字串)。"""
    owner = _create_user()
    event = _create_event(owner, host_email="original@example.com")
    client = _auth_client(owner)

    response = client.patch(
        _detail_url(event.id), {"hostEmail": "not-an-email"}, format="json"
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "HOST_EMAIL_INVALID"
    event.refresh_from_db()
    assert event.host_email == "original@example.com"


def test_patch_host_email_can_differ_from_account_email():
    """⑨ hostEmail 改成與 request.user.email 不同、但格式合法的另一個 Email →
    200,Event.host_email 更新為請求中的新值(確認脫鉤)。"""
    owner = _create_user(email="account-email@example.com")
    event = _create_event(owner, host_email="original-host-email@example.com")
    client = _auth_client(owner)

    response = client.patch(
        _detail_url(event.id),
        {"hostEmail": "new-contact@example.com"},
        format="json",
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["hostEmail"] == "new-contact@example.com"

    event.refresh_from_db()
    assert event.host_email == "new-contact@example.com"
    assert event.host_email != owner.email


def test_patch_nonexistent_event_id_returns_404_with_event_not_found_code():
    """⑩ 對不存在的活動 id 送出編輯 → 404,body 符合 api-error-format 的
    {message, code} 形狀,code 為活動專屬的 "EVENT_NOT_FOUND"。"""
    user = _create_user()
    client = _auth_client(user)

    response = client.patch(
        _detail_url(generate_short_id()), {"title": "無效"}, format="json"
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    body = response.json()
    assert set(body.keys()) == {"message", "code"}
    assert body["code"] == "EVENT_NOT_FOUND"


def test_patch_title_over_30_chars_returns_400():
    """⑪ title 超過 30 字元 → 400,code 為 "TITLE_TOO_LONG"(確認沿用既有長度
    驗證邏輯有正確接上)。"""
    owner = _create_user()
    event = _create_event(owner)
    client = _auth_client(owner)
    original_title = event.title

    response = client.patch(
        _detail_url(event.id), {"title": "揪" * 31}, format="json"
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "TITLE_TOO_LONG"
    event.refresh_from_db()
    assert event.title == original_title


def test_owner_patch_response_with_existing_votes_does_not_n_plus_one():
    """code-review 補充:PATCH 回應也透過 EventDetailSerializer 序列化含
    responses 欄位(同 GET),活動已有投票時不應該為每筆投票的 slots 各多打一次
    查詢——_get_event_or_404 需帶上跟 GET 一樣的 prefetch_related。用固定筆數
    的投票資料跑兩次(2 筆、4 筆),查詢數應相同,證明沒有隨投票筆數線性成長。"""
    owner = _create_user()
    client = _auth_client(owner)

    event_with_two = _create_event(owner)
    slot = event_with_two.slots.first()
    _create_participant_response(event_with_two, "小明", [slot])
    _create_participant_response(event_with_two, "小華", [slot])

    event_with_four = _create_event(owner)
    slot_4 = event_with_four.slots.first()
    for name in ("小明", "小華", "小美", "小強"):
        _create_participant_response(event_with_four, name, [slot_4])

    with CaptureQueriesContext(connection) as captured_two:
        response_two = client.patch(_detail_url(event_with_two.id), {}, format="json")
    with CaptureQueriesContext(connection) as captured_four:
        response_four = client.patch(_detail_url(event_with_four.id), {}, format="json")

    assert response_two.status_code == status.HTTP_200_OK
    assert response_four.status_code == status.HTTP_200_OK
    assert len(response_two.json()["responses"]) == 2
    assert len(response_four.json()["responses"]) == 4
    assert len(captured_two.captured_queries) == len(captured_four.captured_queries)


# ---------------------------------------------------------------------------
# POST /api/events/{id}/responses — 參與者初次投票(add-participant-responses)
# ---------------------------------------------------------------------------


def _responses_url(event_id):
    return f"/api/events/{event_id}/responses/"


def _add_slot(event, date="2026-10-02"):
    return Slot.objects.create(event=event, date=date)


def _response_payload(**overrides):
    payload = {
        "nickname": "小華",
        "phoneLastThree": "123",
        "email": "participant@example.com",
    }
    payload.update(overrides)
    return payload


def _slot_availabilities(available=(), if_needed=(), unavailable=()):
    """組出 ``slotAvailabilities`` 請求陣列,依語意分類要表態的 slot id——
    三態需求(design.md D4 2026-09-21 修訂)要求每次送出都涵蓋該活動全部候選
    時段,呼叫端需自行確保三個分類合計等於該活動的候選時段總數。"""
    items = []
    for slot_id in available:
        items.append({"slotId": str(slot_id), "availability": "available"})
    for slot_id in if_needed:
        items.append({"slotId": str(slot_id), "availability": "if_needed"})
    for slot_id in unavailable:
        items.append({"slotId": str(slot_id), "availability": "unavailable"})
    return items


def _patch_participant_response_id_default(monkeypatch, fake):
    """設定 ``ParticipantResponse.id`` 欄位的 ``default``,理由同
    ``_patch_event_id_default``(Django 把解析後的 default getter 快取在
    ``Field._get_default``,需連快取一起清掉)。"""
    field = ParticipantResponse._meta.get_field("id")
    monkeypatch.setattr(field, "default", fake)
    monkeypatch.delitem(field.__dict__, "_get_default", raising=False)


def test_participant_can_submit_first_vote_successfully():
    """① 合法輸入(暱稱＋手機末三碼＋複選 2 個時段)→ 201,回應含新建 response 的
    id(8 碼 base62 格式),DB 有一筆對應資料,phone_last_three_hash 不等於明碼、
    且能透過 check_password 驗證回原始輸入。"""
    owner = _create_user()
    event = _create_event(owner)
    # slot_1 須在 _add_slot 之前取得——Slot.id 是 UUID,.first() 沒有明確
    # order_by 時不保證回傳最初建立的那筆,活動有 2 個以上 slot 時才會露餡
    # (測到 duplicate slotId 觸發 SLOT_AVAILABILITY_INCOMPLETE 才發現這個既有
    # fixture 寫法的潛在 flaky 點)。
    slot_1 = event.slots.first()
    slot_2 = _add_slot(event)
    client = APIClient()
    slot_ids = [str(slot_1.id), str(slot_2.id)]

    response = client.post(
        _responses_url(event.id),
        _response_payload(
            slotAvailabilities=_slot_availabilities(available=slot_ids)
        ),
        format="json",
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    # 使用者要求:回應改成跟 GET /api/events/{id} 完全一樣的完整活動格式,前端
    # 可直接拿來渲染,不用另外再打一次 GET(design.md D16)。finalAttendees 是
    # 後續追加的欄位(D9,2026-09-24),此處活動未定案應為空陣列。
    assert set(body.keys()) == {
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
        "slotSummary",
        "responses",
        "finalSlotId",
        "finalNote",
        "finalAttendees",
    }
    assert body["id"] == str(event.id)
    assert body["finalSlotId"] is None
    assert body["finalNote"] is None
    assert len(body["responses"]) == 1
    new_response_body = body["responses"][0]
    assert RESPONSE_SHORT_ID_RE.match(new_response_body["id"])
    summary_by_slot_id = {item["slotId"]: item for item in body["slotSummary"]}
    assert set(summary_by_slot_id.keys()) == set(slot_ids)
    for item in summary_by_slot_id.values():
        assert item == {
            "slotId": item["slotId"],
            "available": 1,
            "if_needed": 0,
            "unavailable": 0,
        }

    assert ParticipantResponse.objects.count() == 1
    participant_response = ParticipantResponse.objects.get()
    assert str(participant_response.id) == new_response_body["id"]
    assert participant_response.nickname == "小華"
    assert participant_response.phone_last_three_hash != "123"
    assert check_password("123", participant_response.phone_last_three_hash)
    assert set(str(s) for s in participant_response.slots.values_list("id", flat=True)) == set(
        slot_ids
    )
    assert set(
        participant_response.slot_availabilities.values_list("availability", flat=True)
    ) == {"available"}


def test_participant_vote_without_email_is_allowed():
    """② 選填 email 不帶 → 201,email 為 null。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()
    payload = _response_payload(
        slotAvailabilities=_slot_availabilities(available=[event.slots.first().id])
    )
    del payload["email"]

    response = client.post(_responses_url(event.id), payload, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    participant_response = ParticipantResponse.objects.get()
    assert participant_response.email is None


def test_participant_vote_blank_email_returns_400_with_semantic_code():
    """code-review 補充:email 帶空字串(而非省略或 null)→ 400,code 為
    語意化的 PARTICIPANT_EMAIL_INVALID,不是 DRF 原始未對照的 "blank"。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()
    payload = _response_payload(
        email="",
        slotAvailabilities=_slot_availabilities(available=[event.slots.first().id]),
    )

    response = client.post(_responses_url(event.id), payload, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "PARTICIPANT_EMAIL_INVALID"
    assert ParticipantResponse.objects.count() == 0


def test_participant_vote_nickname_conflicts_with_host_nickname_returns_400():
    """新增:暱稱與主揪 hostNickname 相同(trim 後精確比對)→ 400
    NICKNAME_CONFLICTS_WITH_HOST,不建立任何資料。使用者明確要求新增此規則。"""
    owner = _create_user()
    event = _create_event(owner, host_nickname="小明")
    client = APIClient()

    response = client.post(
        _responses_url(event.id),
        _response_payload(
            nickname="小明",
            slotAvailabilities=_slot_availabilities(available=[event.slots.first().id]),
        ),
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "NICKNAME_CONFLICTS_WITH_HOST"
    assert ParticipantResponse.objects.count() == 0


def test_participant_vote_nickname_conflicts_with_host_nickname_after_trim_returns_400():
    """新增:暱稱前後帶空白,trim 後與主揪 hostNickname 相同 → 同上,400
    NICKNAME_CONFLICTS_WITH_HOST。"""
    owner = _create_user()
    event = _create_event(owner, host_nickname="小明")
    client = APIClient()

    response = client.post(
        _responses_url(event.id),
        _response_payload(
            nickname="  小明  ",
            slotAvailabilities=_slot_availabilities(available=[event.slots.first().id]),
        ),
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "NICKNAME_CONFLICTS_WITH_HOST"
    assert ParticipantResponse.objects.count() == 0


def test_participant_vote_with_comment_is_stored():
    """新增:帶選填 comment 欄位 → 201,DB 該筆投票的 comment 為送出的值。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()

    response = client.post(
        _responses_url(event.id),
        _response_payload(
            comment="期待這次揪團！",
            slotAvailabilities=_slot_availabilities(available=[event.slots.first().id]),
        ),
        format="json",
    )

    assert response.status_code == status.HTTP_201_CREATED
    participant_response = ParticipantResponse.objects.get()
    assert participant_response.comment == "期待這次揪團！"


def test_participant_vote_without_comment_stores_null():
    """新增:不帶 comment → 201,DB 該筆投票的 comment 為 None(跟 email 同款
    「選填不帶則為 null」慣例)。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()

    response = client.post(
        _responses_url(event.id),
        _response_payload(
            slotAvailabilities=_slot_availabilities(available=[event.slots.first().id])
        ),
        format="json",
    )

    assert response.status_code == status.HTTP_201_CREATED
    participant_response = ParticipantResponse.objects.get()
    assert participant_response.comment is None


def test_participant_vote_blank_comment_stores_null():
    """新增:comment 帶空字串 → 201(不是 400,跟 email 的 blank 判定不同——
    comment 沒有格式對錯的概念),DB 存為 None。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()

    response = client.post(
        _responses_url(event.id),
        _response_payload(
            comment="",
            slotAvailabilities=_slot_availabilities(available=[event.slots.first().id]),
        ),
        format="json",
    )

    assert response.status_code == status.HTTP_201_CREATED
    participant_response = ParticipantResponse.objects.get()
    assert participant_response.comment is None


def test_participant_vote_comment_over_200_chars_returns_400():
    """新增:comment 超過 200 字元 → 400 COMMENT_TOO_LONG,不建立任何資料。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()

    response = client.post(
        _responses_url(event.id),
        _response_payload(
            comment="a" * 201,
            slotAvailabilities=_slot_availabilities(available=[event.slots.first().id]),
        ),
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "COMMENT_TOO_LONG"
    assert ParticipantResponse.objects.count() == 0


def test_participant_vote_with_duplicate_nickname_returns_400():
    """③ 暱稱與既有(trim 後)重複 → 400 NICKNAME_TAKEN,DB 未新增。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()
    slot_id = str(event.slots.first().id)
    client.post(
        _responses_url(event.id),
        _response_payload(slotAvailabilities=_slot_availabilities(available=[slot_id])),
        format="json",
    )
    assert ParticipantResponse.objects.count() == 1

    response = client.post(
        _responses_url(event.id),
        _response_payload(slotAvailabilities=_slot_availabilities(available=[slot_id])),
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "NICKNAME_TAKEN"
    assert ParticipantResponse.objects.count() == 1


def test_participant_vote_with_duplicate_nickname_after_trim_returns_400():
    """④ 暱稱前後帶空白但 trim 後與既有重複 → 400 NICKNAME_TAKEN。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()
    slot_id = str(event.slots.first().id)
    client.post(
        _responses_url(event.id),
        _response_payload(
            nickname="小華", slotAvailabilities=_slot_availabilities(available=[slot_id])
        ),
        format="json",
    )

    response = client.post(
        _responses_url(event.id),
        _response_payload(
            nickname="  小華  ",
            slotAvailabilities=_slot_availabilities(available=[slot_id]),
        ),
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "NICKNAME_TAKEN"
    assert ParticipantResponse.objects.count() == 1


def test_participant_vote_missing_required_fields_returns_400():
    """⑤ 暱稱缺漏／手機末三碼缺漏／slotAvailabilities 缺漏 → 400 對應 code。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()
    slot_id = str(event.slots.first().id)

    payload_without_nickname = _response_payload(
        slotAvailabilities=_slot_availabilities(available=[slot_id])
    )
    del payload_without_nickname["nickname"]
    response = client.post(_responses_url(event.id), payload_without_nickname, format="json")
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "NICKNAME_REQUIRED"

    payload_without_phone = _response_payload(
        slotAvailabilities=_slot_availabilities(available=[slot_id])
    )
    del payload_without_phone["phoneLastThree"]
    response = client.post(_responses_url(event.id), payload_without_phone, format="json")
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "PHONE_LAST_THREE_REQUIRED"

    payload_without_slot_availabilities = _response_payload()
    response = client.post(
        _responses_url(event.id), payload_without_slot_availabilities, format="json"
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "SLOT_AVAILABILITIES_REQUIRED"

    assert ParticipantResponse.objects.count() == 0


def test_participant_vote_slot_availabilities_incomplete_returns_400():
    """新增(三態需求,design.md D4 2026-09-21 修訂③):該活動有 2 個候選時段,
    只表態其中 1 個 → 400 SLOT_AVAILABILITY_INCOMPLETE,不建立任何資料。同一個
    slot id 表態兩次(即使另一個時段也有表態,合計筆數超過時段總數)→ 同樣的
    400 SLOT_AVAILABILITY_INCOMPLETE。"""
    owner = _create_user()
    event = _create_event(owner)
    slot_1 = event.slots.first()
    slot_2 = _add_slot(event)
    client = APIClient()

    response = client.post(
        _responses_url(event.id),
        _response_payload(
            slotAvailabilities=_slot_availabilities(available=[slot_1.id])
        ),
        format="json",
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "SLOT_AVAILABILITY_INCOMPLETE"

    response = client.post(
        _responses_url(event.id),
        _response_payload(
            slotAvailabilities=[
                {"slotId": str(slot_1.id), "availability": "available"},
                {"slotId": str(slot_1.id), "availability": "if_needed"},
                {"slotId": str(slot_2.id), "availability": "unavailable"},
            ]
        ),
        format="json",
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "SLOT_AVAILABILITY_INCOMPLETE"

    assert ParticipantResponse.objects.count() == 0


def test_participant_vote_invalid_phone_last_three_returns_400():
    """⑥ 手機末三碼非 3 位數字(帶字母、2 位、4 位)→ 400 PHONE_LAST_THREE_INVALID。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()
    slot_id = str(event.slots.first().id)

    for invalid_phone in ("12a", "12", "1234"):
        response = client.post(
            _responses_url(event.id),
            _response_payload(
                phoneLastThree=invalid_phone,
                slotAvailabilities=_slot_availabilities(available=[slot_id]),
            ),
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["code"] == "PHONE_LAST_THREE_INVALID"

    assert ParticipantResponse.objects.count() == 0


def test_participant_vote_full_width_digit_phone_last_three_returns_400():
    """code-review 補充:手機末三碼帶全形數字(Unicode \\d 會誤判為合法數字)
    → 400 PHONE_LAST_THREE_INVALID,不視為合法的 3 位數字。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()
    slot_id = str(event.slots.first().id)

    response = client.post(
        _responses_url(event.id),
        _response_payload(
            phoneLastThree="１２３",
            slotAvailabilities=_slot_availabilities(available=[slot_id]),
        ),
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "PHONE_LAST_THREE_INVALID"
    assert ParticipantResponse.objects.count() == 0


def test_participant_vote_whitespace_only_nickname_or_phone_returns_required_code():
    """code-review 補充:nickname／phoneLastThree 帶純空白字串(DRF CharField
    trim_whitespace 後視為空)→ 400,code 仍是語意化的 *_REQUIRED,不是 DRF 原始
    的 "blank"。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()
    slot_id = str(event.slots.first().id)

    response = client.post(
        _responses_url(event.id),
        _response_payload(
            nickname="   ", slotAvailabilities=_slot_availabilities(available=[slot_id])
        ),
        format="json",
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "NICKNAME_REQUIRED"

    response = client.post(
        _responses_url(event.id),
        _response_payload(
            phoneLastThree="   ",
            slotAvailabilities=_slot_availabilities(available=[slot_id]),
        ),
        format="json",
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "PHONE_LAST_THREE_REQUIRED"

    assert ParticipantResponse.objects.count() == 0


def test_participant_vote_with_slot_not_belonging_to_event_returns_400():
    """⑦ slotAvailabilities 內含不屬於該活動的 slot id → 400 SLOT_NOT_FOUND,不
    建立任何資料。"""
    owner = _create_user()
    event = _create_event(owner)
    other_event = _create_event(owner)
    client = APIClient()
    foreign_slot_id = str(other_event.slots.first().id)

    response = client.post(
        _responses_url(event.id),
        _response_payload(
            slotAvailabilities=_slot_availabilities(available=[foreign_slot_id])
        ),
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "SLOT_NOT_FOUND"
    assert ParticipantResponse.objects.count() == 0


def test_participant_vote_slot_id_not_uuid_returns_400():
    """新增(對照實測發現):slotAvailabilities[].slotId 不是合法 UUID 字串
    → 400 SLOT_ID_INVALID,不是未對照的 DRF 原始碼 "invalid"，不建立任何資料。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()

    response = client.post(
        _responses_url(event.id),
        _response_payload(
            slotAvailabilities=[{"slotId": "not-a-uuid", "availability": "available"}]
        ),
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "SLOT_ID_INVALID"
    assert ParticipantResponse.objects.count() == 0


def test_participant_vote_availability_invalid_choice_returns_400():
    """新增(三態需求):slotAvailabilities[].availability 不是
    available/if_needed/unavailable 三者之一 → 400 AVAILABILITY_INVALID,不
    建立任何資料。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()

    response = client.post(
        _responses_url(event.id),
        _response_payload(
            slotAvailabilities=[
                {"slotId": str(event.slots.first().id), "availability": "maybe"}
            ]
        ),
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "AVAILABILITY_INVALID"
    assert ParticipantResponse.objects.count() == 0


def test_participant_vote_nonexistent_event_returns_404():
    """⑧ 活動不存在 → 404 EVENT_NOT_FOUND。"""
    client = APIClient()

    response = client.post(
        _responses_url(generate_short_id()),
        _response_payload(
            slotAvailabilities=_slot_availabilities(available=[str(uuid.uuid4())])
        ),
        format="json",
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["code"] == "EVENT_NOT_FOUND"


def test_participant_vote_link_expired_returns_410():
    """⑨ 活動連結已失效(status=cancelled 超過 7 天)→ 410 LINK_EXPIRED,不建立
    任何資料。"""
    owner = _create_user()
    event = _create_event(
        owner,
        status=Event.Status.CANCELLED,
        cancelled_at=timezone.now() - timedelta(days=8),
    )
    client = APIClient()

    response = client.post(
        _responses_url(event.id),
        _response_payload(
            slotAvailabilities=_slot_availabilities(available=[event.slots.first().id])
        ),
        format="json",
    )

    assert response.status_code == status.HTTP_410_GONE
    assert response.json()["code"] == "LINK_EXPIRED"
    assert ParticipantResponse.objects.count() == 0


def test_participant_vote_event_not_active_returns_409():
    """⑩ 活動 status 非 active(取消／定案,未超過 7 天)→ 409 EVENT_NOT_ACTIVE。"""
    owner = _create_user()
    client = APIClient()
    now = timezone.now()

    finalized_event = _create_event(
        owner, status=Event.Status.FINALIZED, finalized_at=now
    )
    finalized_event.final_slot = finalized_event.slots.first()
    finalized_event.save()
    cancelled_event = _create_event(
        owner, status=Event.Status.CANCELLED, cancelled_at=now
    )

    for event in (finalized_event, cancelled_event):
        response = client.post(
            _responses_url(event.id),
            _response_payload(
                slotAvailabilities=_slot_availabilities(
                    available=[event.slots.first().id]
                )
            ),
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()["code"] == "EVENT_NOT_ACTIVE"

    assert ParticipantResponse.objects.count() == 0


def test_participant_vote_voting_closed_returns_409():
    """⑪ 活動 status=active 但 response_deadline 已過 → 409 VOTING_CLOSED。

    曾短暫改成只讓這支端點回 400(D10),使用者事後確認要統一改回 409,跟
    ``EVENT_NOT_ACTIVE``、``verify``/``PATCH`` 兩支參與者端點一致,見 D10
    修訂記錄。"""
    owner = _create_user()
    event = _create_event(owner, response_deadline=timezone.now() - timedelta(hours=1))
    client = APIClient()

    response = client.post(
        _responses_url(event.id),
        _response_payload(
            slotAvailabilities=_slot_availabilities(available=[event.slots.first().id])
        ),
        format="json",
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "VOTING_CLOSED"
    assert ParticipantResponse.objects.count() == 0


def test_participant_response_id_collision_retries_and_still_succeeds(monkeypatch):
    """⑫(D9)用 mock 讓 generate_short_id 前兩次回傳同一個已存在的 id、第三次
    回傳新 id → 仍 201 成功建立(驗證碰撞重試路徑);另外驗證兩個不同暱稱正常
    各自成功時不會誤觸發重試。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()
    slot_id = str(event.slots.first().id)

    # 先建立一筆真實資料,佔用一個 id。暱稱刻意不用 "小明"——那是
    # `_create_event` 預設的 hostNickname,新增的 NICKNAME_CONFLICTS_WITH_HOST
    # 規則會擋下來,這裡要測的是 id 碰撞重試,跟主揪暱稱衝突無關。
    first_response = client.post(
        _responses_url(event.id),
        _response_payload(
            nickname="小美", slotAvailabilities=_slot_availabilities(available=[slot_id])
        ),
        format="json",
    )
    assert first_response.status_code == status.HTTP_201_CREATED
    existing_id = ParticipantResponse.objects.get(nickname="小美").id
    real_generate = generate_short_id
    calls = {"n": 0}

    def colliding_once_then_real():
        calls["n"] += 1
        return existing_id if calls["n"] == 1 else real_generate()

    _patch_participant_response_id_default(monkeypatch, colliding_once_then_real)

    response = client.post(
        _responses_url(event.id),
        _response_payload(
            nickname="小華", slotAvailabilities=_slot_availabilities(available=[slot_id])
        ),
        format="json",
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert calls["n"] >= 2
    new_id = ParticipantResponse.objects.get(nickname="小華").id
    assert new_id != existing_id
    assert ParticipantResponse.objects.count() == 2


def test_participant_response_different_nicknames_do_not_trigger_retry():
    """⑫ 補充:兩個不同暱稱的正常請求各自成功,不會誤觸發 NICKNAME_TAKEN 或
    id 碰撞重試路徑。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()
    slot_id = str(event.slots.first().id)

    # 暱稱刻意不用 "小明"(`_create_event` 預設的 hostNickname),理由同上。
    response_1 = client.post(
        _responses_url(event.id),
        _response_payload(
            nickname="小美", slotAvailabilities=_slot_availabilities(available=[slot_id])
        ),
        format="json",
    )
    response_2 = client.post(
        _responses_url(event.id),
        _response_payload(
            nickname="小華", slotAvailabilities=_slot_availabilities(available=[slot_id])
        ),
        format="json",
    )

    assert response_1.status_code == status.HTTP_201_CREATED
    assert response_2.status_code == status.HTTP_201_CREATED
    assert ParticipantResponse.objects.count() == 2


def _verify_url(event_id):
    return f"/api/events/{event_id}/responses/verify/"


def _create_verifiable_participant_response(event, **overrides):
    """`_create_participant_response`(見上方,Task 2 測試已定義)的簡化版本
    ——固定暱稱「小華」、手機末三碼「123」、勾選 `event` 的第一個 slot,供本節
    身分核對測試重複使用,呼叫端只需視需要覆寫個別欄位。"""
    defaults = {"email": "participant@example.com"}
    defaults.update(overrides)
    return _create_participant_response(event, "小華", [event.slots.first()], **defaults)


def test_participant_verify_identity_success_returns_access_token_and_vote_content():
    """① 正確暱稱＋正確手機末三碼 → 200,回應含 accessToken(明文)、expiresAt、
    該筆投票的 id(供前端拼接後續 PATCH .../responses/{responseId} 的 URL,
    換裝置或清除 localStorage 後仍能繼續操作)、原投票內容
    (nickname/email/slotAvailabilities,供前端預填);DB 新增一筆 token 紀錄,
    token_hash 不等於明碼 accessToken。"""
    owner = _create_user()
    event = _create_event(owner)
    slot_id = str(event.slots.first().id)
    participant_response = _create_verifiable_participant_response(event)
    client = APIClient()

    response = client.post(
        _verify_url(event.id),
        {"nickname": "小華", "phoneLastThree": "123"},
        format="json",
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert set(body.keys()) == {
        "accessToken",
        "expiresAt",
        "id",
        "nickname",
        "email",
        "slotAvailabilities",
    }
    assert body["id"] == participant_response.id
    assert isinstance(body["accessToken"], str) and body["accessToken"]
    assert body["nickname"] == "小華"
    assert body["email"] == "participant@example.com"
    assert body["slotAvailabilities"] == [
        {"slotId": slot_id, "availability": "available"}
    ]

    assert ParticipantResponseAccessToken.objects.count() == 1
    token_record = ParticipantResponseAccessToken.objects.get()
    assert token_record.response_id == participant_response.id
    assert token_record.token_hash != body["accessToken"]
    assert body["accessToken"] not in token_record.token_hash
    assert token_record.used_at is None


def test_participant_verify_nonexistent_nickname_returns_401():
    """② 暱稱不存在 → 401 IDENTITY_VERIFICATION_FAILED,不核發存取憑證。"""
    owner = _create_user()
    event = _create_event(owner)
    _create_verifiable_participant_response(event)
    client = APIClient()

    response = client.post(
        _verify_url(event.id),
        {"nickname": "沒有這個人", "phoneLastThree": "123"},
        format="json",
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json()["code"] == "IDENTITY_VERIFICATION_FAILED"
    assert ParticipantResponseAccessToken.objects.count() == 0


def test_participant_verify_wrong_phone_last_three_returns_same_401_body_as_missing_nickname():
    """③ 暱稱存在但手機末三碼錯誤 → 401 IDENTITY_VERIFICATION_FAILED,與②回應
    body 完全相同,驗證不洩漏差異;不核發存取憑證。"""
    owner = _create_user()
    event = _create_event(owner)
    _create_verifiable_participant_response(event)
    client = APIClient()

    wrong_phone_response = client.post(
        _verify_url(event.id),
        {"nickname": "小華", "phoneLastThree": "999"},
        format="json",
    )
    missing_nickname_response = client.post(
        _verify_url(event.id),
        {"nickname": "沒有這個人", "phoneLastThree": "123"},
        format="json",
    )

    assert wrong_phone_response.status_code == status.HTTP_401_UNAUTHORIZED
    assert wrong_phone_response.json()["code"] == "IDENTITY_VERIFICATION_FAILED"
    assert wrong_phone_response.json() == missing_nickname_response.json()
    assert ParticipantResponseAccessToken.objects.count() == 0


def test_participant_verify_nonexistent_event_returns_404():
    """④ 活動不存在 → 404 EVENT_NOT_FOUND,沿用共用前置檢查函式。"""
    client = APIClient()

    response = client.post(
        _verify_url(generate_short_id()),
        {"nickname": "小華", "phoneLastThree": "123"},
        format="json",
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["code"] == "EVENT_NOT_FOUND"


def test_participant_verify_link_expired_returns_410():
    """④ 活動連結已失效(status=cancelled 超過 7 天)→ 410 LINK_EXPIRED。"""
    owner = _create_user()
    event = _create_event(
        owner,
        status=Event.Status.CANCELLED,
        cancelled_at=timezone.now() - timedelta(days=8),
    )
    _create_verifiable_participant_response(event)
    client = APIClient()

    response = client.post(
        _verify_url(event.id),
        {"nickname": "小華", "phoneLastThree": "123"},
        format="json",
    )

    assert response.status_code == status.HTTP_410_GONE
    assert response.json()["code"] == "LINK_EXPIRED"
    assert ParticipantResponseAccessToken.objects.count() == 0


def test_participant_verify_event_not_active_returns_409():
    """④ 活動 status 非 active(取消,未超過 7 天)→ 409 EVENT_NOT_ACTIVE。"""
    owner = _create_user()
    event = _create_event(
        owner, status=Event.Status.CANCELLED, cancelled_at=timezone.now()
    )
    _create_verifiable_participant_response(event)
    client = APIClient()

    response = client.post(
        _verify_url(event.id),
        {"nickname": "小華", "phoneLastThree": "123"},
        format="json",
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "EVENT_NOT_ACTIVE"
    assert ParticipantResponseAccessToken.objects.count() == 0


def test_participant_verify_voting_closed_returns_409():
    """④ 活動 status=active 但 response_deadline 已過 → 409 VOTING_CLOSED。"""
    owner = _create_user()
    event = _create_event(owner, response_deadline=timezone.now() - timedelta(hours=1))
    _create_verifiable_participant_response(event)
    client = APIClient()

    response = client.post(
        _verify_url(event.id),
        {"nickname": "小華", "phoneLastThree": "123"},
        format="json",
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "VOTING_CLOSED"
    assert ParticipantResponseAccessToken.objects.count() == 0


# ---------------------------------------------------------------------------
# PATCH /api/events/{id}/responses/{responseId} — 參與者更新投票(add-participant-responses)
# ---------------------------------------------------------------------------


def _patch_response_url(event_id, response_id):
    return f"/api/events/{event_id}/responses/{response_id}/"


def _issue_access_token(participant_response, **overrides):
    """核發一組測試用存取憑證：比照 view 端 ``_hash_participant_access_token``
    (sha256、不加 salt)手動建立 DB 紀錄,回傳明文 token 供測試組請求 body。"""
    plaintext_token = overrides.pop("plaintext_token", secrets.token_urlsafe(32))
    defaults = {
        "response": participant_response,
        "token_hash": hashlib.sha256(plaintext_token.encode()).hexdigest(),
        "expires_at": timezone.now() + timedelta(minutes=30),
    }
    defaults.update(overrides)
    ParticipantResponseAccessToken.objects.create(**defaults)
    return plaintext_token


def test_participant_patch_with_valid_token_updates_slots_only():
    """① 帶有效未過期未使用的 token,修改 slotAvailabilities → 200,DB 該筆
    投票的表態已更新為新內容,nickname/email/phone_last_three_hash 皆未變動。"""
    owner = _create_user()
    event = _create_event(owner)
    slot_1 = event.slots.first()
    slot_2 = _add_slot(event, date="2026-10-03")
    participant_response = _create_participant_response(
        event, "小華", [slot_1], email="participant@example.com"
    )
    original_phone_hash = participant_response.phone_last_three_hash
    token = _issue_access_token(participant_response)
    client = APIClient()

    response = client.patch(
        _patch_response_url(event.id, participant_response.id),
        {
            "accessToken": token,
            "slotAvailabilities": _slot_availabilities(
                available=[slot_2.id], unavailable=[slot_1.id]
            ),
        },
        format="json",
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    # 同 create 端點:回應改成完整 GET /api/events/{id} 格式(design.md D16)。
    assert body["id"] == str(event.id)
    response_item = next(
        item for item in body["responses"] if item["id"] == participant_response.id
    )
    assert {
        (item["slotId"], item["availability"])
        for item in response_item["slotAvailabilities"]
    } == {(str(slot_1.id), "unavailable"), (str(slot_2.id), "available")}

    participant_response.refresh_from_db()
    assert set(
        str(s) for s in participant_response.slots.values_list("id", flat=True)
    ) == {str(slot_1.id), str(slot_2.id)}
    assert {
        (str(a.slot_id), a.availability)
        for a in participant_response.slot_availabilities.all()
    } == {(str(slot_1.id), "unavailable"), (str(slot_2.id), "available")}
    assert participant_response.nickname == "小華"
    assert participant_response.email == "participant@example.com"
    assert participant_response.phone_last_three_hash == original_phone_hash


def test_participant_patch_bumps_updated_at_for_polling():
    """add-event-poll:成功改票後 updated_at 要比改票前新，供 /poll 端點的
    latestResponseAt 偵測「有投票被修改」（不是只偵測到「新投票」）。"""
    owner = _create_user()
    event = _create_event(owner)
    slot_1 = event.slots.first()
    slot_2 = _add_slot(event, date="2026-10-03")
    participant_response = _create_participant_response(event, "小華", [slot_1])
    original_updated_at = participant_response.updated_at
    token = _issue_access_token(participant_response)
    client = APIClient()

    response = client.patch(
        _patch_response_url(event.id, participant_response.id),
        {
            "accessToken": token,
            "slotAvailabilities": _slot_availabilities(
                available=[slot_2.id], unavailable=[slot_1.id]
            ),
        },
        format="json",
    )

    assert response.status_code == status.HTTP_200_OK
    participant_response.refresh_from_db()
    assert participant_response.updated_at > original_updated_at


def test_participant_patch_token_already_used_returns_401():
    """② token 使用後再次帶同一個 token 送出 → 401 ACCESS_TOKEN_INVALID(一次性
    驗證),第二次請求不再變動資料。"""
    owner = _create_user()
    event = _create_event(owner)
    slot_1 = event.slots.first()
    slot_2 = _add_slot(event, date="2026-10-03")
    participant_response = _create_participant_response(event, "小華", [slot_1])
    token = _issue_access_token(participant_response)
    client = APIClient()

    first_response = client.patch(
        _patch_response_url(event.id, participant_response.id),
        {
            "accessToken": token,
            "slotAvailabilities": _slot_availabilities(
                available=[slot_2.id], unavailable=[slot_1.id]
            ),
        },
        format="json",
    )
    assert first_response.status_code == status.HTTP_200_OK

    second_response = client.patch(
        _patch_response_url(event.id, participant_response.id),
        {
            "accessToken": token,
            "slotAvailabilities": _slot_availabilities(
                available=[slot_1.id], unavailable=[slot_2.id]
            ),
        },
        format="json",
    )

    assert second_response.status_code == status.HTTP_401_UNAUTHORIZED
    assert second_response.json()["code"] == "ACCESS_TOKEN_INVALID"
    participant_response.refresh_from_db()
    assert {
        (str(a.slot_id), a.availability)
        for a in participant_response.slot_availabilities.all()
    } == {(str(slot_1.id), "unavailable"), (str(slot_2.id), "available")}


@pytest.mark.django_db(transaction=True)
def test_participant_patch_concurrent_requests_with_same_token_only_one_succeeds():
    """code-review 補充:兩個請求幾乎同時帶著同一個有效 token 送出 PATCH,
    DB 層級的 compare-and-swap(``UPDATE ... WHERE used_at IS NULL``)必須保證
    只有一個真的成功消費 token、寫入表態——不能只靠 Python 物件裡讀到的舊值
    判斷(見 views.py 的 ``claimed`` 計數)。用 ``transaction=True`` 讓兩個執行緒
    各自拿到真正獨立的 DB connection,才測得出真實的併發行為。"""
    import threading

    owner = _create_user()
    event = _create_event(owner)
    slot_1 = event.slots.first()
    slot_2 = _add_slot(event, date="2026-10-03")
    participant_response = _create_participant_response(event, "小華", [slot_1])
    token = _issue_access_token(participant_response)

    status_codes = []
    start_barrier = threading.Barrier(2)

    def send_patch(target_slot_id, other_slot_id):
        start_barrier.wait()
        client = APIClient()
        try:
            response = client.patch(
                _patch_response_url(event.id, participant_response.id),
                {
                    "accessToken": token,
                    "slotAvailabilities": _slot_availabilities(
                        available=[target_slot_id], unavailable=[other_slot_id]
                    ),
                },
                format="json",
            )
            status_codes.append(response.status_code)
        finally:
            # transaction=True 讓每個執行緒拿到獨立的 DB connection——測試結束
            # 後不主動關閉,pytest-django 拆測試 DB 時會因為連線還在用而炸掉。
            connection.close()

    threads = [
        threading.Thread(target=send_patch, args=(slot_1.id, slot_2.id)),
        threading.Thread(target=send_patch, args=(slot_2.id, slot_1.id)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(status_codes) == [
        status.HTTP_200_OK,
        status.HTTP_401_UNAUTHORIZED,
    ]
    token_record = ParticipantResponseAccessToken.objects.get(response=participant_response)
    assert token_record.used_at is not None


def test_participant_patch_expired_token_returns_401():
    """③ token 已過期(直接建立 expires_at 為過去的測試資料)→ 401
    ACCESS_TOKEN_INVALID,不更動資料。"""
    owner = _create_user()
    event = _create_event(owner)
    slot_1 = event.slots.first()
    slot_2 = _add_slot(event, date="2026-10-03")
    participant_response = _create_participant_response(event, "小華", [slot_1])
    token = _issue_access_token(
        participant_response, expires_at=timezone.now() - timedelta(minutes=1)
    )
    client = APIClient()

    response = client.patch(
        _patch_response_url(event.id, participant_response.id),
        {
            "accessToken": token,
            "slotAvailabilities": _slot_availabilities(
                available=[slot_2.id], unavailable=[slot_1.id]
            ),
        },
        format="json",
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json()["code"] == "ACCESS_TOKEN_INVALID"
    participant_response.refresh_from_db()
    assert set(
        str(s) for s in participant_response.slots.values_list("id", flat=True)
    ) == {str(slot_1.id)}


def test_participant_patch_token_expiring_between_precheck_and_claim_returns_401(
    monkeypatch,
):
    """Codex 二次審查抓到:早期檢查(patch() 開頭那段)判斷 token 未過期後,
    到真正的 compare-and-swap UPDATE 之間如果 token 剛好過期,消費當下必須
    仍判定為過期、拒絕消費——不能因為早期檢查用的是舊的 now 而僥倖成功。

    用真實時間流逝驗證(不 mock timezone.now,避免遮蔽 Django/DRF 內部其他
    呼叫點):token 效期設為 100 毫秒,在 `_check_participation_preconditions`
    (早期檢查之後、CAS 之前的呼叫點)人為注入 300 毫秒延遲,讓 token 確實在
    這段空檔到期。"""
    import apps.events.views as events_views

    owner = _create_user()
    event = _create_event(owner)
    slot_1 = event.slots.first()
    participant_response = _create_participant_response(event, "小華", [slot_1])
    token = _issue_access_token(
        participant_response, expires_at=timezone.now() + timedelta(milliseconds=100)
    )

    real_check_preconditions = events_views._check_participation_preconditions

    def slow_check_preconditions(*args, **kwargs):
        time.sleep(0.3)
        return real_check_preconditions(*args, **kwargs)

    monkeypatch.setattr(
        events_views, "_check_participation_preconditions", slow_check_preconditions
    )
    client = APIClient()

    response = client.patch(
        _patch_response_url(event.id, participant_response.id),
        {
            "accessToken": token,
            "slotAvailabilities": _slot_availabilities(available=[slot_1.id]),
        },
        format="json",
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json()["code"] == "ACCESS_TOKEN_INVALID"
    token_record = ParticipantResponseAccessToken.objects.get(response=participant_response)
    assert token_record.used_at is None


def test_participant_patch_nonexistent_or_malformed_token_returns_401():
    """④ token 不存在／格式錯誤 → 401 ACCESS_TOKEN_INVALID。涵蓋:完全隨機、
    非該 event 核發過任何 token 的字串;請求根本沒帶 accessToken 欄位。"""
    owner = _create_user()
    event = _create_event(owner)
    slot_1 = event.slots.first()
    participant_response = _create_participant_response(event, "小華", [slot_1])
    client = APIClient()

    response = client.patch(
        _patch_response_url(event.id, participant_response.id),
        {
            "accessToken": "not-a-real-token",
            "slotAvailabilities": _slot_availabilities(available=[slot_1.id]),
        },
        format="json",
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json()["code"] == "ACCESS_TOKEN_INVALID"

    response = client.patch(
        _patch_response_url(event.id, participant_response.id),
        {"slotAvailabilities": _slot_availabilities(available=[slot_1.id])},
        format="json",
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json()["code"] == "ACCESS_TOKEN_INVALID"


def test_participant_patch_token_belongs_to_another_response_returns_401():
    """⑤ token 屬於另一筆 response,拿來改這筆的 responseId → 401
    ACCESS_TOKEN_INVALID,不更動資料,對應 token 未被消費。"""
    owner = _create_user()
    event = _create_event(owner)
    slot_1 = event.slots.first()
    slot_2 = _add_slot(event, date="2026-10-03")
    response_a = _create_participant_response(event, "小華", [slot_1])
    response_b = _create_participant_response(event, "小美", [slot_1])
    token_for_a = _issue_access_token(response_a)
    client = APIClient()

    response = client.patch(
        _patch_response_url(event.id, response_b.id),
        {
            "accessToken": token_for_a,
            "slotAvailabilities": _slot_availabilities(
                available=[slot_2.id], unavailable=[slot_1.id]
            ),
        },
        format="json",
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json()["code"] == "ACCESS_TOKEN_INVALID"
    response_b.refresh_from_db()
    assert set(
        str(s) for s in response_b.slots.values_list("id", flat=True)
    ) == {str(slot_1.id)}
    token_record = ParticipantResponseAccessToken.objects.get(response=response_a)
    assert token_record.used_at is None


def test_participant_patch_ignores_locked_fields():
    """⑥ body 帶 nickname/email/phoneLastThree 企圖修改 → 皆被忽略,DB 對應欄位
    不變(僅 slotAvailabilities 生效)。"""
    owner = _create_user()
    event = _create_event(owner)
    slot_1 = event.slots.first()
    slot_2 = _add_slot(event, date="2026-10-03")
    participant_response = _create_participant_response(
        event, "小華", [slot_1], email="participant@example.com"
    )
    original_phone_hash = participant_response.phone_last_three_hash
    token = _issue_access_token(participant_response)
    client = APIClient()

    response = client.patch(
        _patch_response_url(event.id, participant_response.id),
        {
            "accessToken": token,
            "slotAvailabilities": _slot_availabilities(
                available=[slot_2.id], unavailable=[slot_1.id]
            ),
            "nickname": "偷改暱稱",
            "email": "hacker@example.com",
            "phoneLastThree": "999",
        },
        format="json",
    )

    assert response.status_code == status.HTTP_200_OK
    participant_response.refresh_from_db()
    assert participant_response.nickname == "小華"
    assert participant_response.email == "participant@example.com"
    assert participant_response.phone_last_three_hash == original_phone_hash
    assert set(
        str(s) for s in participant_response.slots.values_list("id", flat=True)
    ) == {str(slot_1.id), str(slot_2.id)}


def test_participant_patch_with_slot_not_belonging_to_event_returns_400():
    """⑦ slotAvailabilities 含不屬於該活動的 slot id → 400 SLOT_NOT_FOUND,DB
    未變動,token 未被消費。"""
    owner = _create_user()
    event = _create_event(owner)
    other_event = _create_event(owner)
    slot_1 = event.slots.first()
    participant_response = _create_participant_response(event, "小華", [slot_1])
    token = _issue_access_token(participant_response)
    foreign_slot_id = str(other_event.slots.first().id)
    client = APIClient()

    response = client.patch(
        _patch_response_url(event.id, participant_response.id),
        {
            "accessToken": token,
            "slotAvailabilities": _slot_availabilities(available=[foreign_slot_id]),
        },
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "SLOT_NOT_FOUND"
    participant_response.refresh_from_db()
    assert set(
        str(s) for s in participant_response.slots.values_list("id", flat=True)
    ) == {str(slot_1.id)}
    token_record = ParticipantResponseAccessToken.objects.get()
    assert token_record.used_at is None


def test_participant_patch_slot_id_not_uuid_returns_400():
    """新增(對照實測發現):slotAvailabilities[].slotId 不是合法 UUID 字串 →
    400 SLOT_ID_INVALID,DB 未變動,token 未被消費。"""
    owner = _create_user()
    event = _create_event(owner)
    slot_1 = event.slots.first()
    participant_response = _create_participant_response(event, "小華", [slot_1])
    token = _issue_access_token(participant_response)
    client = APIClient()

    response = client.patch(
        _patch_response_url(event.id, participant_response.id),
        {
            "accessToken": token,
            "slotAvailabilities": [{"slotId": "not-a-uuid", "availability": "available"}],
        },
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "SLOT_ID_INVALID"
    participant_response.refresh_from_db()
    assert set(
        str(s) for s in participant_response.slots.values_list("id", flat=True)
    ) == {str(slot_1.id)}
    token_record = ParticipantResponseAccessToken.objects.get()
    assert token_record.used_at is None


def test_participant_patch_slot_availabilities_incomplete_returns_400():
    """新增(三態需求,design.md D4 2026-09-21 修訂③):活動有 2 個候選時段,
    PATCH 只表態其中 1 個 → 400 SLOT_AVAILABILITY_INCOMPLETE,DB 未變動,token
    未被消費。"""
    owner = _create_user()
    event = _create_event(owner)
    slot_1 = event.slots.first()
    slot_2 = _add_slot(event, date="2026-10-03")
    participant_response = _create_participant_response(
        event, "小華", {slot_1: "available", slot_2: "unavailable"}
    )
    token = _issue_access_token(participant_response)
    client = APIClient()

    response = client.patch(
        _patch_response_url(event.id, participant_response.id),
        {
            "accessToken": token,
            "slotAvailabilities": _slot_availabilities(available=[slot_2.id]),
        },
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "SLOT_AVAILABILITY_INCOMPLETE"
    assert {
        (str(a.slot_id), a.availability)
        for a in participant_response.slot_availabilities.all()
    } == {(str(slot_1.id), "available"), (str(slot_2.id), "unavailable")}
    token_record = ParticipantResponseAccessToken.objects.get()
    assert token_record.used_at is None


def test_participant_patch_link_expired_returns_410_and_token_unconsumed():
    """⑧ 活動連結已失效(status=cancelled 超過 7 天)→ 沿用共用前置檢查,410
    LINK_EXPIRED,token 未被消費。"""
    owner = _create_user()
    event = _create_event(
        owner,
        status=Event.Status.CANCELLED,
        cancelled_at=timezone.now() - timedelta(days=8),
    )
    slot_1 = event.slots.first()
    participant_response = _create_participant_response(event, "小華", [slot_1])
    token = _issue_access_token(participant_response)
    client = APIClient()

    response = client.patch(
        _patch_response_url(event.id, participant_response.id),
        {
            "accessToken": token,
            "slotAvailabilities": _slot_availabilities(available=[slot_1.id]),
        },
        format="json",
    )

    assert response.status_code == status.HTTP_410_GONE
    assert response.json()["code"] == "LINK_EXPIRED"
    token_record = ParticipantResponseAccessToken.objects.get()
    assert token_record.used_at is None


def test_participant_patch_event_not_active_returns_409_and_token_unconsumed():
    """⑧ 活動 status 非 active(取消,未超過 7 天)→ 沿用共用前置檢查,409
    EVENT_NOT_ACTIVE,token 未被消費。"""
    owner = _create_user()
    event = _create_event(
        owner, status=Event.Status.CANCELLED, cancelled_at=timezone.now()
    )
    slot_1 = event.slots.first()
    participant_response = _create_participant_response(event, "小華", [slot_1])
    token = _issue_access_token(participant_response)
    client = APIClient()

    response = client.patch(
        _patch_response_url(event.id, participant_response.id),
        {
            "accessToken": token,
            "slotAvailabilities": _slot_availabilities(available=[slot_1.id]),
        },
        format="json",
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "EVENT_NOT_ACTIVE"
    token_record = ParticipantResponseAccessToken.objects.get()
    assert token_record.used_at is None


def test_participant_patch_voting_closed_returns_409_and_token_unconsumed():
    """⑧ 活動 status=active 但 response_deadline 已過 → 沿用共用前置檢查,409
    VOTING_CLOSED,token 未被消費。"""
    owner = _create_user()
    event = _create_event(owner, response_deadline=timezone.now() - timedelta(hours=1))
    slot_1 = event.slots.first()
    participant_response = _create_participant_response(event, "小華", [slot_1])
    token = _issue_access_token(participant_response)
    client = APIClient()

    response = client.patch(
        _patch_response_url(event.id, participant_response.id),
        {
            "accessToken": token,
            "slotAvailabilities": _slot_availabilities(available=[slot_1.id]),
        },
        format="json",
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "VOTING_CLOSED"
    token_record = ParticipantResponseAccessToken.objects.get()
    assert token_record.used_at is None


# ---------------------------------------------------------------------------
# POST/GET /api/events/{id}/comments (add-event-comments)
# ---------------------------------------------------------------------------


def _comments_url(event_id):
    return f"/api/events/{event_id}/comments/"


def _comment_payload(**overrides):
    payload = {"nickname": "小華", "message": "期待這次聚會！"}
    payload.update(overrides)
    return payload


def test_comment_can_be_posted_successfully():
    """① 合法暱稱＋內容 → 201,回應含 id(8 碼 base62)/nickname/message/
    createdAt,DB 有一筆對應資料。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()

    response = client.post(_comments_url(event.id), _comment_payload(), format="json")

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert set(body.keys()) == {"id", "nickname", "message", "createdAt"}
    assert RESPONSE_SHORT_ID_RE.match(body["id"])
    assert body["nickname"] == "小華"
    assert body["message"] == "期待這次聚會！"

    assert Comment.objects.count() == 1
    comment = Comment.objects.get()
    assert str(comment.id) == body["id"]
    assert comment.event_id == event.id


def test_comment_nickname_is_trimmed():
    """② 暱稱前後帶空白,trim 後儲存。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()

    response = client.post(
        _comments_url(event.id),
        _comment_payload(nickname="  小華  "),
        format="json",
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["nickname"] == "小華"
    assert Comment.objects.get().nickname == "小華"


def test_comment_missing_nickname_returns_400():
    """③ 暱稱缺漏 → 400 NICKNAME_REQUIRED,不建立任何資料。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()
    payload = _comment_payload()
    del payload["nickname"]

    response = client.post(_comments_url(event.id), payload, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "NICKNAME_REQUIRED"
    assert Comment.objects.count() == 0


def test_comment_missing_message_returns_400():
    """④ 內容缺漏 → 400 MESSAGE_REQUIRED,不建立任何資料。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()
    payload = _comment_payload()
    del payload["message"]

    response = client.post(_comments_url(event.id), payload, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "MESSAGE_REQUIRED"
    assert Comment.objects.count() == 0


def test_comment_message_over_200_chars_returns_400():
    """⑤ 內容超過 200 字 → 400 MESSAGE_TOO_LONG,不建立任何資料。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()

    response = client.post(
        _comments_url(event.id),
        _comment_payload(message="a" * 201),
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "MESSAGE_TOO_LONG"
    assert Comment.objects.count() == 0


def test_comment_allowed_when_event_finalized_or_cancelled():
    """⑥ 活動 status 為 finalized/cancelled(未超過 7 天)仍可成功留言——留言的
    前提條件只看連結是否失效,不看活動狀態(design.md D5)。"""
    owner = _create_user()
    client = APIClient()
    now = timezone.now()

    finalized_event = _create_event(
        owner, status=Event.Status.FINALIZED, finalized_at=now
    )
    finalized_event.final_slot = finalized_event.slots.first()
    finalized_event.save()
    cancelled_event = _create_event(owner, status=Event.Status.CANCELLED, cancelled_at=now)

    for event in (finalized_event, cancelled_event):
        response = client.post(
            _comments_url(event.id), _comment_payload(), format="json"
        )
        assert response.status_code == status.HTTP_201_CREATED

    assert Comment.objects.count() == 2


def test_comment_link_expired_returns_410():
    """⑦ 活動連結已失效(status=cancelled 超過 7 天)→ 410 LINK_EXPIRED,不建立
    任何資料。"""
    owner = _create_user()
    event = _create_event(
        owner,
        status=Event.Status.CANCELLED,
        cancelled_at=timezone.now() - timedelta(days=8),
    )
    client = APIClient()

    response = client.post(_comments_url(event.id), _comment_payload(), format="json")

    assert response.status_code == status.HTTP_410_GONE
    assert response.json()["code"] == "LINK_EXPIRED"
    assert Comment.objects.count() == 0


def test_comment_list_link_expired_returns_410():
    """code-review 補充:GET 端也要驗證連結已失效的 410 分支——原本只有
    POST 那條測到,GET 用同一個 ``_display_status_or_410`` 呼叫卻沒有直接
    測試涵蓋。"""
    owner = _create_user()
    event = _create_event(
        owner,
        status=Event.Status.CANCELLED,
        cancelled_at=timezone.now() - timedelta(days=8),
    )
    client = APIClient()

    response = client.get(_comments_url(event.id))

    assert response.status_code == status.HTTP_410_GONE
    assert response.json()["code"] == "LINK_EXPIRED"


def test_comment_post_nonexistent_event_returns_404():
    """⑧ 活動不存在 → 404 EVENT_NOT_FOUND。"""
    client = APIClient()

    response = client.post(
        _comments_url(generate_short_id()), _comment_payload(), format="json"
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["code"] == "EVENT_NOT_FOUND"


def test_comment_same_nickname_can_post_multiple_times():
    """⑨ 同一暱稱可連續留言兩次,皆成功——留言不要求活動內暱稱唯一
    (design.md D6,跟 ParticipantResponse.nickname 的唯一限制不同)。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()

    first = client.post(
        _comments_url(event.id), _comment_payload(message="第一則"), format="json"
    )
    second = client.post(
        _comments_url(event.id), _comment_payload(message="第二則"), format="json"
    )

    assert first.status_code == status.HTTP_201_CREATED
    assert second.status_code == status.HTTP_201_CREATED
    assert Comment.objects.filter(event=event, nickname="小華").count() == 2


def _patch_comment_id_default(monkeypatch, fake):
    """設定 ``Comment.id`` 欄位的 ``default``,理由同 ``_patch_event_id_default``
    (Django 把解析後的 default getter 快取在 ``Field._get_default``,需連快取
    一起清掉)。"""
    field = Comment._meta.get_field("id")
    monkeypatch.setattr(field, "default", fake)
    monkeypatch.delitem(field.__dict__, "_get_default", raising=False)


def test_comment_id_collision_retries_and_still_succeeds(monkeypatch):
    """⑩ Comment.id 產生器撞到既有 id 時重試,換到不重複的 id 後仍建立成功。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()
    existing = Comment.objects.create(event=event, nickname="小美", message="先佔一個 id")
    real_generate = generate_short_id
    calls = {"n": 0}

    def colliding_once_then_real():
        calls["n"] += 1
        return existing.id if calls["n"] == 1 else real_generate()

    _patch_comment_id_default(monkeypatch, colliding_once_then_real)

    response = client.post(_comments_url(event.id), _comment_payload(), format="json")

    assert response.status_code == status.HTTP_201_CREATED
    assert calls["n"] >= 2
    assert Comment.objects.count() == 2


def test_comment_list_returns_all_sorted_by_created_at_ascending():
    """⑪ 活動有 3 則留言(刻意用不同的建立順序/created_at)→ 200,回應陣列依
    created_at 由舊到新排序。``created_at`` 是 ``auto_now_add``,``.create()``
    時傳入的值會被忽略、強制寫成當下時間——建立後改用 ``.update()``(繞過
    ``auto_now_add`` 的 ``pre_save``,只有 ``.save()``/``.create()`` 才會觸發)
    才能真正控制每筆的時間,驗證排序不是碰巧跟建立順序一致。"""
    owner = _create_user()
    event = _create_event(owner)
    now = timezone.now()
    third = Comment.objects.create(event=event, nickname="小美", message="第三則")
    first = Comment.objects.create(event=event, nickname="小華", message="第一則")
    second = Comment.objects.create(event=event, nickname="小明", message="第二則")
    Comment.objects.filter(pk=third.pk).update(created_at=now)
    Comment.objects.filter(pk=first.pk).update(created_at=now - timedelta(minutes=10))
    Comment.objects.filter(pk=second.pk).update(created_at=now - timedelta(minutes=5))
    first.refresh_from_db()
    client = APIClient()

    response = client.get(_comments_url(event.id))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert [item["id"] for item in body] == [first.id, second.id, third.id]
    assert body[0]["id"] == first.id
    assert body[0]["nickname"] == "小華"
    assert body[0]["message"] == "第一則"
    assert dateparse.parse_datetime(body[0]["createdAt"]) == first.created_at


def test_comment_list_returns_empty_array_when_no_comments():
    """⑫ 活動無留言 → 200,空陣列。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()

    response = client.get(_comments_url(event.id))

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == []


def test_comment_list_nonexistent_event_returns_404():
    """⑬ 活動不存在 → 404 EVENT_NOT_FOUND。"""
    client = APIClient()

    response = client.get(_comments_url(generate_short_id()))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["code"] == "EVENT_NOT_FOUND"


# ---------------------------------------------------------------------------
# DELETE /api/events/{id}/comments/{commentId} (add-event-comments D9)
# ---------------------------------------------------------------------------


def _comment_delete_url(event_id, comment_id):
    return f"/api/events/{event_id}/comments/{comment_id}/"


def test_comment_owner_can_delete_own_event_comment():
    """① 主揪本人刪除存在且未刪除的留言 → 204,空 body,該留言之後不再出現在
    GET 列表。"""
    owner = _create_user()
    event = _create_event(owner)
    comment = Comment.objects.create(event=event, nickname="小華", message="哈囉")
    client = _auth_client(owner)

    response = client.delete(_comment_delete_url(event.id, comment.id))

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert response.data is None
    comment.refresh_from_db()
    assert comment.deleted_at is not None

    list_response = APIClient().get(_comments_url(event.id))
    assert list_response.json() == []


def test_comment_delete_by_non_owner_returns_403():
    """② 已登入但非擁有者刪除 → 403 FORBIDDEN,留言不受影響(比照
    test_non_owner_authenticated_user_cannot_patch_event 同款寫法)。"""
    owner = _create_user()
    other_user = _create_user(email="other@example.com", google_sub="sub-other")
    event = _create_event(owner)
    comment = Comment.objects.create(event=event, nickname="小華", message="哈囉")
    client = _auth_client(other_user)

    response = client.delete(_comment_delete_url(event.id, comment.id))

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["code"] == "FORBIDDEN"
    comment.refresh_from_db()
    assert comment.deleted_at is None


def test_comment_delete_unauthenticated_returns_401():
    """②之二 未登入(不帶 token)刪除 → 401,留言不受影響(比照
    test_unauthenticated_user_cannot_patch_event 同款寫法)。"""
    owner = _create_user()
    event = _create_event(owner)
    comment = Comment.objects.create(event=event, nickname="小華", message="哈囉")
    client = APIClient()

    response = client.delete(_comment_delete_url(event.id, comment.id))

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    comment.refresh_from_db()
    assert comment.deleted_at is None


def test_comment_delete_nonexistent_comment_returns_404():
    """③ 刪除不存在的留言 id → 404 COMMENT_NOT_FOUND。"""
    owner = _create_user()
    event = _create_event(owner)
    client = _auth_client(owner)

    response = client.delete(_comment_delete_url(event.id, generate_short_id()))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["code"] == "COMMENT_NOT_FOUND"


def test_comment_delete_already_deleted_returns_404():
    """④ 對已刪除過的留言再次刪除 → 404 COMMENT_NOT_FOUND,不重複標記。"""
    owner = _create_user()
    event = _create_event(owner)
    comment = Comment.objects.create(
        event=event, nickname="小華", message="哈囉", deleted_at=timezone.now()
    )
    client = _auth_client(owner)

    response = client.delete(_comment_delete_url(event.id, comment.id))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["code"] == "COMMENT_NOT_FOUND"


def test_comment_delete_comment_belonging_to_another_event_returns_404():
    """⑤ 用另一場活動的 id 搭配這場活動的留言 id → 404 COMMENT_NOT_FOUND。"""
    owner = _create_user()
    event_a = _create_event(owner)
    event_b = _create_event(owner, title="另一場活動")
    comment = Comment.objects.create(event=event_b, nickname="小華", message="哈囉")
    client = _auth_client(owner)

    response = client.delete(_comment_delete_url(event_a.id, comment.id))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["code"] == "COMMENT_NOT_FOUND"
    comment.refresh_from_db()
    assert comment.deleted_at is None


def test_comment_list_excludes_deleted_comments():
    """⑥ 活動有 2 則留言、其中 1 則已刪除 → 列表只回未刪除的那 1 則。"""
    owner = _create_user()
    event = _create_event(owner)
    visible = Comment.objects.create(event=event, nickname="小華", message="還在")
    Comment.objects.create(
        event=event, nickname="小美", message="被刪了", deleted_at=timezone.now()
    )
    client = APIClient()

    response = client.get(_comments_url(event.id))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert [item["id"] for item in body] == [visible.id]


@pytest.mark.django_db(transaction=True)
def test_comment_delete_concurrent_requests_only_one_succeeds():
    """code-review 補充:兩個請求幾乎同時對同一則留言送出 DELETE,DB 層級的
    compare-and-swap(``UPDATE ... WHERE deleted_at IS NULL``)必須保證只有一個
    真的成功——不能只靠 Python 物件裡讀到的舊值判斷(同款問題先前已在
    ParticipantResponseDetailView.patch() 的 token 消費修過一次,見 design.md
    D9)。用 ``transaction=True`` 讓兩個執行緒各自拿到真正獨立的 DB
    connection,才測得出真實的併發行為。"""
    import threading

    owner = _create_user()
    event = _create_event(owner)
    comment = Comment.objects.create(event=event, nickname="小華", message="哈囉")

    status_codes = []
    start_barrier = threading.Barrier(2)

    def send_delete():
        start_barrier.wait()
        client = _auth_client(owner)
        try:
            response = client.delete(_comment_delete_url(event.id, comment.id))
            status_codes.append(response.status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=send_delete) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(status_codes) == [
        status.HTTP_204_NO_CONTENT,
        status.HTTP_404_NOT_FOUND,
    ]
    comment.refresh_from_db()
    assert comment.deleted_at is not None


# ---------------------------------------------------------------------------
# POST /api/events/{id}/finalize (add-event-lifecycle)
# ---------------------------------------------------------------------------


def _finalize_url(event_id):
    return f"/api/events/{event_id}/finalize/"


def test_owner_can_finalize_active_event():
    """① 合法 finalSlotId(＋選填 finalNote)→ 200,回應為完整活動格式,
    status="finalized",DB 該活動的 finalized_at/final_slot/final_note 正確
    寫入。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    client = _auth_client(owner)

    response = client.post(
        _finalize_url(event.id),
        {"finalSlotId": str(slot.id), "finalNote": "記得帶睡袋"},
        format="json",
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "finalized"
    assert body["finalSlotId"] == str(slot.id)
    assert body["finalNote"] == "記得帶睡袋"
    event.refresh_from_db()
    assert event.status == Event.Status.FINALIZED
    assert event.final_slot_id == slot.id
    assert event.final_note == "記得帶睡袋"
    assert event.finalized_at is not None


def test_finalized_event_finalAttendees_only_lists_available_for_final_slot():
    """D9(2026-09-24):活動定案後,GET 回應新增 finalAttendees 欄位,只列出對
    「定案時段」表態 available 的人(嚴格定義,if_needed 不算)——前端不用自己
    拿 finalSlotId 比對 responses[].slotAvailabilities。不影響 responses 本身
    的既有完整格式。"""
    owner = _create_user()
    event = _create_event(owner)
    slot_a = event.slots.first()
    slot_b = Slot.objects.create(event=event, date="2026-10-02")

    available_for_a = _create_participant_response(
        event, "小美", {slot_a: "available", slot_b: "unavailable"}
    )
    if_needed_for_a = _create_participant_response(
        event, "小華", {slot_a: "if_needed", slot_b: "available"}
    )
    available_for_b_only = _create_participant_response(
        event, "阿明", {slot_a: "unavailable", slot_b: "available"}
    )

    client = _auth_client(owner)
    response = client.post(
        _finalize_url(event.id), {"finalSlotId": str(slot_a.id)}, format="json"
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["finalSlotId"] == str(slot_a.id)
    attendee_ids = {attendee["id"] for attendee in body["finalAttendees"]}
    assert attendee_ids == {available_for_a.id}
    assert if_needed_for_a.id not in attendee_ids
    assert available_for_b_only.id not in attendee_ids
    attendee = body["finalAttendees"][0]
    assert set(attendee.keys()) == {"id", "nickname", "comment"}
    assert attendee["nickname"] == "小美"


def test_active_event_finalAttendees_is_empty_list():
    """未定案(status=active,final_slot 為 None)時,finalAttendees 回傳空陣列,
    不是 null——欄位型別一致,前端不用多判斷 null 分支。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    _create_participant_response(event, "小美", {slot: "available"})
    client = _auth_client(owner)

    response = client.get(_detail_url(event.id))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["finalAttendees"] == []


def test_finalAttendees_is_empty_when_status_not_finalized_despite_stale_final_slot():
    """code-review 抓到:原本只檢查 final_slot_id,沒核對 status——正常 API
    路徑 cancel/reopen 都會一併清空 final_slot,不會出現這種不一致資料,但為
    了忠於 spec(「僅當活動已定案時」)直接用 ORM 造出 status="active" 但
    final_slot 殘留的資料,確認這種防禦性情況下 finalAttendees 仍為空陣列。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    _create_participant_response(event, "小美", {slot: "available"})
    event.final_slot = slot
    event.save()
    client = _auth_client(owner)

    response = client.get(_detail_url(event.id))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["finalAttendees"] == []


def test_owner_can_finalize_without_final_note():
    """finalNote 選填,不帶時 final_note 為 None。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    client = _auth_client(owner)

    response = client.post(
        _finalize_url(event.id), {"finalSlotId": str(slot.id)}, format="json"
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["finalNote"] is None
    event.refresh_from_db()
    assert event.final_note is None


def test_finalize_by_non_owner_returns_403():
    """② 已登入但非擁有者 → 403 FORBIDDEN,活動不受影響。"""
    owner = _create_user()
    other_user = _create_user(email="other@example.com", google_sub="sub-other")
    event = _create_event(owner)
    slot = event.slots.first()
    client = _auth_client(other_user)

    response = client.post(
        _finalize_url(event.id), {"finalSlotId": str(slot.id)}, format="json"
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["code"] == "FORBIDDEN"
    event.refresh_from_db()
    assert event.status == Event.Status.ACTIVE


def test_finalize_unauthenticated_returns_401():
    """③ 未登入 → 401。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    client = APIClient()

    response = client.post(
        _finalize_url(event.id), {"finalSlotId": str(slot.id)}, format="json"
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    event.refresh_from_db()
    assert event.status == Event.Status.ACTIVE


def test_finalize_slot_not_belonging_to_event_returns_400():
    """④ finalSlotId 不屬於該活動 → 400 SLOT_NOT_FOUND。"""
    owner = _create_user()
    event = _create_event(owner)
    other_event = _create_event(owner, title="另一場活動")
    other_slot = other_event.slots.first()
    client = _auth_client(owner)

    response = client.post(
        _finalize_url(event.id), {"finalSlotId": str(other_slot.id)}, format="json"
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "SLOT_NOT_FOUND"
    event.refresh_from_db()
    assert event.status == Event.Status.ACTIVE


def test_finalize_missing_final_slot_id_returns_400():
    """⑤ finalSlotId 缺漏 → 400。"""
    owner = _create_user()
    event = _create_event(owner)
    client = _auth_client(owner)

    response = client.post(_finalize_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_finalize_note_over_200_chars_returns_400():
    """⑥ finalNote 超過 200 字 → 400。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    client = _auth_client(owner)

    response = client.post(
        _finalize_url(event.id),
        {"finalSlotId": str(slot.id), "finalNote": "a" * 201},
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_finalize_already_finalized_event_returns_409():
    """⑦ 活動已經是 finalized → 409 EVENT_ALREADY_FINALIZED。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    now = timezone.now()
    event.status = Event.Status.FINALIZED
    event.final_slot = slot
    event.finalized_at = now
    event.save()
    client = _auth_client(owner)

    response = client.post(
        _finalize_url(event.id), {"finalSlotId": str(slot.id)}, format="json"
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "EVENT_ALREADY_FINALIZED"


def test_finalize_cancelled_event_returns_409_already_cancelled():
    """⑧ 活動已經是 cancelled → 409 EVENT_ALREADY_CANCELLED。"""
    owner = _create_user()
    event = _create_event(
        owner, status=Event.Status.CANCELLED, cancelled_at=timezone.now()
    )
    slot = event.slots.first()
    client = _auth_client(owner)

    response = client.post(
        _finalize_url(event.id), {"finalSlotId": str(slot.id)}, format="json"
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "EVENT_ALREADY_CANCELLED"


def test_finalize_cancelled_and_link_expired_event_returns_409_not_410():
    """code-review 補充:finalize 不呼叫 _display_status_or_410——連結是否
    失效不影響擁有者對自己活動的定案/取消/重開操作(design.md 2026-09-24
    修訂)。即使活動已取消超過 7 天(連結已失效),回應仍是 409
    EVENT_ALREADY_CANCELLED,不是 410 LINK_EXPIRED。"""
    owner = _create_user()
    event = _create_event(
        owner,
        status=Event.Status.CANCELLED,
        cancelled_at=timezone.now() - timedelta(days=8),
    )
    slot = event.slots.first()
    client = _auth_client(owner)

    response = client.post(
        _finalize_url(event.id), {"finalSlotId": str(slot.id)}, format="json"
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "EVENT_ALREADY_CANCELLED"


def test_owner_can_finalize_event_active_for_more_than_7_days():
    """code-review 補充:_display_status_or_410 拿掉後,active 活動即使
    response_deadline 已過很久(voting_closed_pending,不會變成
    link_expired——active 分支本來就不會產生這個顯示狀態)仍可正常定案，
    不受影響。這條原本就會過，補上是為了明確記錄「finalize 不受連結失效
    邏輯影響」這個修正後的行為。"""
    owner = _create_user()
    event = _create_event(
        owner, response_deadline=timezone.now() - timedelta(days=30)
    )
    slot = event.slots.first()
    client = _auth_client(owner)

    response = client.post(
        _finalize_url(event.id), {"finalSlotId": str(slot.id)}, format="json"
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "finalized"


def test_finalize_nonexistent_event_returns_404():
    """⑩ 活動不存在 → 404 EVENT_NOT_FOUND。"""
    owner = _create_user()
    client = _auth_client(owner)

    response = client.post(
        _finalize_url(generate_short_id()),
        {"finalSlotId": str(uuid.uuid4())},
        format="json",
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["code"] == "EVENT_NOT_FOUND"


def test_finalize_sends_email_to_participants_with_email_and_host(
    django_capture_on_commit_callbacks, mailoutbox
):
    """⑪ 定案成功後,留 Email 的參與者與主揪本人各收到一封通知信,沒留 Email
    的參與者不會收到。code-review 抓到:每封信只能有一個收件人,不可讓參與者
    看到其他參與者/主揪的 Email(隱私外洩)。"""
    owner = _create_user(email="host@example.com")
    event = _create_event(owner, host_email="host@example.com")
    slot = event.slots.first()
    _create_participant_response(
        event, "小華", [slot], email="voter@example.com"
    )
    _create_participant_response(event, "小美", [slot], email=None)
    client = _auth_client(owner)

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            _finalize_url(event.id), {"finalSlotId": str(slot.id)}, format="json"
        )

    assert response.status_code == status.HTTP_200_OK
    assert all(len(mail.to) == 1 for mail in mailoutbox)
    recipients = {addr for mail in mailoutbox for addr in mail.to}
    assert recipients == {"voter@example.com", "host@example.com"}


def test_finalize_succeeds_even_if_notification_dispatch_raises(
    django_capture_on_commit_callbacks, monkeypatch
):
    """使用者實測發現:本機 CELERY_TASK_ALWAYS_EAGER=True 時,
    send_event_finalized_email.delay() 若在 on_commit 執行當下拋例外(例如
    email backend 設定錯誤),整個 view 會被拖累成 500——即使定案本身（DB
    寫入）已經在 on_commit 觸發前就 commit 成功。通知信失敗不該讓一個已經
    成功的動作看起來像失敗，見 design.md Risks「Email 寄送失敗不會讓 API
    請求本身失敗」的既有設計意圖。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    client = _auth_client(owner)

    def _raise(*args, **kwargs):
        raise RuntimeError("email backend misconfigured")

    monkeypatch.setattr(
        "apps.events.views.send_event_finalized_email.delay", _raise
    )

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            _finalize_url(event.id), {"finalSlotId": str(slot.id)}, format="json"
        )

    assert response.status_code == status.HTTP_200_OK
    event.refresh_from_db()
    assert event.status == Event.Status.FINALIZED


# ---------------------------------------------------------------------------
# POST /api/events/{id}/cancel (add-event-lifecycle)
# ---------------------------------------------------------------------------


def _cancel_url(event_id):
    return f"/api/events/{event_id}/cancel/"


def test_owner_can_cancel_active_event_with_votes():
    """① 主揪成功取消進行中且已有投票的活動 → 200,status="cancelled",DB
    該活動全部 ParticipantResponse.deleted_at 皆非空,GET /api/events/{id}
    的 responses/slotSummary 反映為空/全 0。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    _create_participant_response(event, "小華", [slot], email="voter@example.com")
    client = _auth_client(owner)

    response = client.post(_cancel_url(event.id), format="json")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["responses"] == []
    assert body["slotSummary"] == [
        {"slotId": str(slot.id), "available": 0, "if_needed": 0, "unavailable": 0}
    ]
    event.refresh_from_db()
    assert event.status == Event.Status.CANCELLED
    assert event.cancelled_at is not None
    responses = ParticipantResponse.objects.filter(event=event)
    assert responses.count() == 1
    assert all(r.deleted_at is not None for r in responses)


def test_owner_can_cancel_finalized_event():
    """② 主揪成功取消已定案的活動 → 200。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    event.status = Event.Status.FINALIZED
    event.final_slot = slot
    event.finalized_at = timezone.now()
    event.save()
    client = _auth_client(owner)

    response = client.post(_cancel_url(event.id), format="json")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "cancelled"


def test_cancel_by_non_owner_returns_403():
    """③ 非擁有者(已登入)→ 403 FORBIDDEN。"""
    owner = _create_user()
    other_user = _create_user(email="other@example.com", google_sub="sub-other")
    event = _create_event(owner)
    client = _auth_client(other_user)

    response = client.post(_cancel_url(event.id), format="json")

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["code"] == "FORBIDDEN"
    event.refresh_from_db()
    assert event.status == Event.Status.ACTIVE


def test_cancel_unauthenticated_returns_401():
    """④ 未登入 → 401。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()

    response = client.post(_cancel_url(event.id), format="json")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    event.refresh_from_db()
    assert event.status == Event.Status.ACTIVE


def test_cancel_already_cancelled_event_returns_409():
    """⑤ 活動已經是 cancelled → 409 EVENT_ALREADY_CANCELLED,投票資料不受
    影響(deleted_at 維持原狀,不重複標記)。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    participant_response = _create_participant_response(event, "小華", [slot])
    original_deleted_at = timezone.now() - timedelta(days=1)
    ParticipantResponse.objects.filter(pk=participant_response.pk).update(
        deleted_at=original_deleted_at
    )
    event.status = Event.Status.CANCELLED
    event.cancelled_at = timezone.now()
    event.save()
    client = _auth_client(owner)

    response = client.post(_cancel_url(event.id), format="json")

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "EVENT_ALREADY_CANCELLED"
    participant_response.refresh_from_db()
    assert participant_response.deleted_at == original_deleted_at


def test_cancel_cancelled_and_link_expired_event_returns_409_not_410():
    """code-review 補充:cancel 不呼叫 _display_status_or_410（design.md
    2026-09-24 修訂），理由同 finalize。即使活動已取消超過 7 天，回應仍是
    409 EVENT_ALREADY_CANCELLED，不是 410 LINK_EXPIRED。"""
    owner = _create_user()
    event = _create_event(
        owner,
        status=Event.Status.CANCELLED,
        cancelled_at=timezone.now() - timedelta(days=8),
    )
    client = _auth_client(owner)

    response = client.post(_cancel_url(event.id), format="json")

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "EVENT_ALREADY_CANCELLED"


def test_owner_can_cancel_event_finalized_more_than_7_days_ago():
    """code-review 抓到的真實 bug(已修正):原本 cancel 會先呼叫
    _display_status_or_410，一筆已定案超過 7 天(定案本身，不是聚會日期)
    的活動 displayStatus 會算成 link_expired，導致主揪永遠無法取消一筆
    「已經定案一段時間」的活動——即使聚會其實還沒發生。這條 CAS 前提只看
    status 是否為 active/finalized，不看連結是否失效。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    _finalize_event(event, slot)
    Event.objects.filter(pk=event.id).update(
        finalized_at=timezone.now() - timedelta(days=8)
    )
    client = _auth_client(owner)

    response = client.post(_cancel_url(event.id), format="json")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "cancelled"


def test_cancel_nonexistent_event_returns_404():
    """⑦ 活動不存在 → 404 EVENT_NOT_FOUND。"""
    owner = _create_user()
    client = _auth_client(owner)

    response = client.post(_cancel_url(generate_short_id()), format="json")

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["code"] == "EVENT_NOT_FOUND"


def test_cancel_does_not_affect_existing_comments():
    """⑧ 取消不影響既有留言(GET /api/events/{id}/comments 仍看得到)。"""
    owner = _create_user()
    event = _create_event(owner)
    Comment.objects.create(event=event, nickname="小美", message="還在")
    client = _auth_client(owner)

    response = client.post(_cancel_url(event.id), format="json")

    assert response.status_code == status.HTTP_200_OK
    comments_response = APIClient().get(_comments_url(event.id))
    assert len(comments_response.json()) == 1


def test_cancel_sends_email_to_participants_even_when_votes_soft_deleted(
    django_capture_on_commit_callbacks, mailoutbox
):
    """⑨ 取消成功後,原本留 Email 的參與者(即使投票已被軟刪除)與主揪本人各
    收到一封取消通知信。每封信只能有一個收件人(隱私外洩防護,同 finalize)。"""
    owner = _create_user(email="host@example.com")
    event = _create_event(owner, host_email="host@example.com")
    slot = event.slots.first()
    _create_participant_response(
        event, "小華", [slot], email="voter@example.com"
    )
    client = _auth_client(owner)

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(_cancel_url(event.id), format="json")

    assert response.status_code == status.HTTP_200_OK
    assert all(len(mail.to) == 1 for mail in mailoutbox)
    recipients = {addr for mail in mailoutbox for addr in mail.to}
    assert recipients == {"voter@example.com", "host@example.com"}


def test_cancel_succeeds_even_if_notification_dispatch_raises(
    django_capture_on_commit_callbacks, monkeypatch
):
    """同 finalize 版本，使用者實測發現的同款問題。"""
    owner = _create_user()
    event = _create_event(owner)
    client = _auth_client(owner)

    def _raise(*args, **kwargs):
        raise RuntimeError("email backend misconfigured")

    monkeypatch.setattr("apps.events.views.send_event_cancelled_email.delay", _raise)

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(_cancel_url(event.id), format="json")

    assert response.status_code == status.HTTP_200_OK
    event.refresh_from_db()
    assert event.status == Event.Status.CANCELLED


@pytest.mark.django_db(transaction=True)
def test_cancel_concurrent_requests_only_one_succeeds():
    """⑩ 兩個並發取消請求同一活動,只有一個成功(200),另一個 409(比照
    ParticipantResponseDetailView.patch()/CommentDetailView.delete() 的既有
    併發測試寫法,design.md D7)。"""
    import threading

    owner = _create_user()
    event = _create_event(owner)

    status_codes = []
    start_barrier = threading.Barrier(2)

    def send_cancel():
        start_barrier.wait()
        client = _auth_client(owner)
        try:
            response = client.post(_cancel_url(event.id), format="json")
            status_codes.append(response.status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=send_cancel) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(status_codes) == [
        status.HTTP_200_OK,
        status.HTTP_409_CONFLICT,
    ]
    event.refresh_from_db()
    assert event.status == Event.Status.CANCELLED


def test_cancel_finalized_event_clears_final_fields():
    """code-review 補充:取消一筆已定案的活動,final_slot/final_note/
    finalized_at 都要清空,不能留著跟 status="cancelled" 矛盾的舊定案資訊。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    event.status = Event.Status.FINALIZED
    event.final_slot = slot
    event.final_note = "記得帶睡袋"
    event.finalized_at = timezone.now()
    event.save()
    client = _auth_client(owner)

    response = client.post(_cancel_url(event.id), format="json")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["finalSlotId"] is None
    assert body["finalNote"] is None
    event.refresh_from_db()
    assert event.final_slot_id is None
    assert event.final_note is None
    assert event.finalized_at is None


def test_finalize_by_non_owner_on_link_expired_event_returns_403_not_410():
    """code-review 補充:非擁有者對一筆連結已失效的活動送出定案請求,應該先
    擋在擁有者權限檢查(403),不該先回 410——比照 EventDetailView.patch()
    擁有者檢查優先的既有慣例(design.md D8)。"""
    owner = _create_user()
    other_user = _create_user(email="other@example.com", google_sub="sub-other")
    event = _create_event(
        owner,
        status=Event.Status.CANCELLED,
        cancelled_at=timezone.now() - timedelta(days=8),
    )
    slot = event.slots.first()
    client = _auth_client(other_user)

    response = client.post(
        _finalize_url(event.id), {"finalSlotId": str(slot.id)}, format="json"
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["code"] == "FORBIDDEN"


def test_cancel_by_non_owner_on_link_expired_event_returns_403_not_410():
    """code-review 補充:非擁有者對一筆連結已失效的活動送出取消請求,應該先
    擋在擁有者權限檢查(403),不該先回 410。"""
    owner = _create_user()
    other_user = _create_user(email="other@example.com", google_sub="sub-other")
    event = _create_event(
        owner,
        status=Event.Status.CANCELLED,
        cancelled_at=timezone.now() - timedelta(days=8),
    )
    client = _auth_client(other_user)

    response = client.post(_cancel_url(event.id), format="json")

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["code"] == "FORBIDDEN"


# ---------------------------------------------------------------------------
# POST /api/events/{id}/reopen (add-event-reopen)
# ---------------------------------------------------------------------------


def _reopen_url(event_id):
    return f"/api/events/{event_id}/reopen/"


def _finalize_event(event, slot, note=None):
    """直接走 ORM 把 event 定案，供 reopen 測試準備前置狀態，不透過 API。"""
    event.status = Event.Status.FINALIZED
    event.final_slot = slot
    event.final_note = note
    event.finalized_at = timezone.now()
    event.save()
    return event


def test_owner_can_reopen_finalized_event():
    """① 主揪成功重新開放已定案活動 → 200，回應 status="active"、新
    responseDeadline、finalSlotId/finalNote 皆為 null，DB 正確寫入，既有
    投票紀錄不受影響。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    _create_participant_response(event, "小華", [slot], email="voter@example.com")
    _finalize_event(event, slot, note="記得帶睡袋")
    new_deadline = timezone.now() + timedelta(days=5)
    client = _auth_client(owner)

    response = client.post(
        _reopen_url(event.id),
        {"responseDeadline": new_deadline.isoformat()},
        format="json",
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "active"
    assert body["finalSlotId"] is None
    assert body["finalNote"] is None
    assert len(body["responses"]) == 1

    event.refresh_from_db()
    assert event.status == Event.Status.ACTIVE
    assert event.final_slot_id is None
    assert event.final_note is None
    assert event.finalized_at is None
    assert ParticipantResponse.objects.filter(event=event, deleted_at__isnull=True).count() == 1


def test_reopen_by_non_owner_returns_403():
    """② 已登入但非擁有者 → 403 FORBIDDEN。"""
    owner = _create_user()
    other_user = _create_user(email="other@example.com", google_sub="sub-other")
    event = _create_event(owner)
    slot = event.slots.first()
    _finalize_event(event, slot)
    client = _auth_client(other_user)

    response = client.post(
        _reopen_url(event.id),
        {"responseDeadline": (timezone.now() + timedelta(days=5)).isoformat()},
        format="json",
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["code"] == "FORBIDDEN"
    event.refresh_from_db()
    assert event.status == Event.Status.FINALIZED


def test_reopen_unauthenticated_returns_401():
    """③ 未登入 → 401。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    _finalize_event(event, slot)
    client = APIClient()

    response = client.post(
        _reopen_url(event.id),
        {"responseDeadline": (timezone.now() + timedelta(days=5)).isoformat()},
        format="json",
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    event.refresh_from_db()
    assert event.status == Event.Status.FINALIZED


def test_reopen_deadline_in_past_returns_400():
    """④ 新截止時間早於/等於現在 → 400 DEADLINE_IN_PAST。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    _finalize_event(event, slot)
    client = _auth_client(owner)

    response = client.post(
        _reopen_url(event.id),
        {"responseDeadline": (timezone.now() - timedelta(days=1)).isoformat()},
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "DEADLINE_IN_PAST"
    event.refresh_from_db()
    assert event.status == Event.Status.FINALIZED


def test_reopen_missing_deadline_returns_400():
    """⑤ 新截止時間缺漏 → 400 RESPONSE_DEADLINE_REQUIRED。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    _finalize_event(event, slot)
    client = _auth_client(owner)

    response = client.post(_reopen_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "RESPONSE_DEADLINE_REQUIRED"


def test_reopen_active_event_returns_409():
    """⑥ 對 active 活動重新開放 → 409 EVENT_NOT_FINALIZED。"""
    owner = _create_user()
    event = _create_event(owner)
    client = _auth_client(owner)

    response = client.post(
        _reopen_url(event.id),
        {"responseDeadline": (timezone.now() + timedelta(days=5)).isoformat()},
        format="json",
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "EVENT_NOT_FINALIZED"


def test_reopen_cancelled_event_returns_409():
    """⑦ 對 cancelled 活動重新開放 → 409 EVENT_NOT_FINALIZED。"""
    owner = _create_user()
    event = _create_event(
        owner, status=Event.Status.CANCELLED, cancelled_at=timezone.now()
    )
    client = _auth_client(owner)

    response = client.post(
        _reopen_url(event.id),
        {"responseDeadline": (timezone.now() + timedelta(days=5)).isoformat()},
        format="json",
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "EVENT_NOT_FINALIZED"


def test_reopen_cancelled_and_link_expired_event_returns_409_not_410():
    """code-review 補充:reopen 不呼叫 _display_status_or_410（design.md
    2026-09-24 修訂），理由同 finalize/cancel。即使活動已取消超過 7 天，
    回應仍是 409 EVENT_NOT_FINALIZED，不是 410 LINK_EXPIRED。"""
    owner = _create_user()
    event = _create_event(
        owner,
        status=Event.Status.CANCELLED,
        cancelled_at=timezone.now() - timedelta(days=8),
    )
    client = _auth_client(owner)

    response = client.post(
        _reopen_url(event.id),
        {"responseDeadline": (timezone.now() + timedelta(days=5)).isoformat()},
        format="json",
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "EVENT_NOT_FINALIZED"


def test_owner_can_reopen_event_finalized_more_than_7_days_ago():
    """code-review 抓到的真實 bug(已修正):原本 reopen 會先呼叫
    _display_status_or_410，一筆已定案超過 7 天的活動 displayStatus 會算
    成 link_expired，導致 reopen 對它唯一有意義的目標對象（已定案一段時間
    的活動）永遠回 410，功能形同無法使用——這正是 code-review 抓到的問題，
    也是本次要修的核心情境。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    _finalize_event(event, slot)
    Event.objects.filter(pk=event.id).update(
        finalized_at=timezone.now() - timedelta(days=8)
    )
    client = _auth_client(owner)

    response = client.post(
        _reopen_url(event.id),
        {"responseDeadline": (timezone.now() + timedelta(days=5)).isoformat()},
        format="json",
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "active"


def test_reopen_nonexistent_event_returns_404():
    """⑨ 活動不存在 → 404 EVENT_NOT_FOUND。"""
    owner = _create_user()
    client = _auth_client(owner)

    response = client.post(
        _reopen_url(generate_short_id()),
        {"responseDeadline": (timezone.now() + timedelta(days=5)).isoformat()},
        format="json",
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["code"] == "EVENT_NOT_FOUND"


def test_reopen_sends_email_to_participants_with_email_and_host(
    django_capture_on_commit_callbacks, mailoutbox
):
    """⑩ 重新開放成功後,留 Email 的參與者與主揪本人各收到一封通知信。每封信
    只能有一個收件人(隱私外洩防護,同 finalize)。"""
    owner = _create_user(email="host@example.com")
    event = _create_event(owner, host_email="host@example.com")
    slot = event.slots.first()
    _create_participant_response(event, "小華", [slot], email="voter@example.com")
    _finalize_event(event, slot)
    client = _auth_client(owner)

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            _reopen_url(event.id),
            {"responseDeadline": (timezone.now() + timedelta(days=5)).isoformat()},
            format="json",
        )

    assert response.status_code == status.HTTP_200_OK
    assert all(len(mail.to) == 1 for mail in mailoutbox)
    recipients = {addr for mail in mailoutbox for addr in mail.to}
    assert recipients == {"voter@example.com", "host@example.com"}


def test_reopen_succeeds_even_if_notification_dispatch_raises(
    django_capture_on_commit_callbacks, monkeypatch
):
    """⑪ 通知信 .delay() 拋例外時 API 仍回 200（比照 add-event-lifecycle
    2026-09-23 修訂的既有回歸測試寫法）。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    _finalize_event(event, slot)
    client = _auth_client(owner)

    def _raise(*args, **kwargs):
        raise RuntimeError("email backend misconfigured")

    monkeypatch.setattr("apps.events.views.send_event_reopened_email.delay", _raise)

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            _reopen_url(event.id),
            {"responseDeadline": (timezone.now() + timedelta(days=5)).isoformat()},
            format="json",
        )

    assert response.status_code == status.HTTP_200_OK
    event.refresh_from_db()
    assert event.status == Event.Status.ACTIVE


@pytest.mark.django_db(transaction=True)
def test_reopen_concurrent_requests_only_one_succeeds():
    """⑫ 兩個並發重新開放請求同一活動,只有一個成功(200),另一個 409。"""
    import threading

    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    _finalize_event(event, slot)

    status_codes = []
    start_barrier = threading.Barrier(2)

    def send_reopen():
        start_barrier.wait()
        client = _auth_client(owner)
        try:
            response = client.post(
                _reopen_url(event.id),
                {
                    "responseDeadline": (
                        timezone.now() + timedelta(days=5)
                    ).isoformat()
                },
                format="json",
            )
            status_codes.append(response.status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=send_reopen) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(status_codes) == [
        status.HTTP_200_OK,
        status.HTTP_409_CONFLICT,
    ]
    event.refresh_from_db()
    assert event.status == Event.Status.ACTIVE


def _poll_url(event_id):
    return f"/api/events/{event_id}/poll/"


def test_poll_returns_counts_and_latest_timestamps_for_responses_and_comments():
    """① 有投票與留言的活動 → 200，responseCount/commentCount/latestResponseAt/
    latestCommentAt 正確反映真實資料。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    _create_participant_response(event, "小華", [slot])
    _create_participant_response(event, "小美", [slot])
    Comment.objects.create(event=event, nickname="小明", message="哈囉")
    latest_comment = Comment.objects.create(event=event, nickname="小李", message="期待")
    client = APIClient()

    response = client.get(_poll_url(event.id))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert set(body.keys()) == {
        "status",
        "displayStatus",
        "eventUpdatedAt",
        "responseCount",
        "latestResponseAt",
        "commentCount",
        "latestCommentAt",
    }
    assert body["status"] == "active"
    assert body["displayStatus"] == "voting_open"
    assert body["responseCount"] == 2
    assert body["commentCount"] == 2
    assert body["latestResponseAt"] is not None
    assert body["latestCommentAt"] is not None
    latest_comment.refresh_from_db()
    assert body["latestCommentAt"] == latest_comment.created_at.isoformat().replace(
        "+00:00", "Z"
    )


def test_poll_returns_zero_counts_and_null_timestamps_when_empty():
    """② 完全沒有投票也沒有留言 → count 皆 0，時間皆 null。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()

    response = client.get(_poll_url(event.id))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["responseCount"] == 0
    assert body["commentCount"] == 0
    assert body["latestResponseAt"] is None
    assert body["latestCommentAt"] is None


def test_poll_reflects_edited_response_without_changing_count():
    """③ 改票後 latestResponseAt 更新、responseCount 不變（依賴 Seam 1 的
    updated_at）。"""
    owner = _create_user()
    event = _create_event(owner)
    slot_1 = event.slots.first()
    slot_2 = _add_slot(event, date="2026-10-03")
    participant_response = _create_participant_response(event, "小華", [slot_1])
    token = _issue_access_token(participant_response)
    client = APIClient()

    before = client.get(_poll_url(event.id)).json()

    client.patch(
        _patch_response_url(event.id, participant_response.id),
        {
            "accessToken": token,
            "slotAvailabilities": _slot_availabilities(
                available=[slot_2.id], unavailable=[slot_1.id]
            ),
        },
        format="json",
    )
    after = client.get(_poll_url(event.id)).json()

    assert after["responseCount"] == before["responseCount"] == 1
    assert after["latestResponseAt"] != before["latestResponseAt"]


def test_poll_excludes_soft_deleted_responses_and_reflects_cancelled_status():
    """④ 已取消的活動（既有投票軟刪除）：responseCount 排除被軟刪除的投票，
    status/displayStatus 反映 cancelled。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    client = _auth_client(owner)
    _create_participant_response(event, "小華", [slot])

    response = client.post(f"/api/events/{event.id}/cancel/", {}, format="json")
    assert response.status_code == status.HTTP_200_OK

    poll_response = APIClient().get(_poll_url(event.id))

    assert poll_response.status_code == status.HTTP_200_OK
    body = poll_response.json()
    assert body["status"] == "cancelled"
    assert body["displayStatus"] == "cancelled"
    assert body["responseCount"] == 0
    assert body["latestResponseAt"] is None


def test_poll_excludes_soft_deleted_comments():
    """⑤ 留言被軟刪除後 commentCount 排除、latestCommentAt 不算入該則。"""
    owner = _create_user()
    event = _create_event(owner)
    Comment.objects.create(
        event=event, nickname="小華", message="哈囉", deleted_at=timezone.now()
    )
    client = APIClient()

    response = client.get(_poll_url(event.id))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["commentCount"] == 0
    assert body["latestCommentAt"] is None


def test_poll_reflects_finalized_status():
    """⑥ 定案後 status/displayStatus 正確反映。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    client = _auth_client(owner)

    response = client.post(
        f"/api/events/{event.id}/finalize/",
        {"finalSlotId": str(slot.id)},
        format="json",
    )
    assert response.status_code == status.HTTP_200_OK

    poll_response = APIClient().get(_poll_url(event.id))

    assert poll_response.status_code == status.HTTP_200_OK
    body = poll_response.json()
    assert body["status"] == "finalized"
    assert body["displayStatus"] == "finalized_upcoming"


def test_poll_reflects_reopened_status():
    """⑥b 重新開放後 status/displayStatus 正確反映回 active/voting_open。"""
    owner = _create_user()
    event = _create_event(
        owner,
        status=Event.Status.FINALIZED,
        finalized_at=timezone.now(),
    )
    slot = event.slots.first()
    event.final_slot = slot
    event.save(update_fields=["final_slot"])
    client = _auth_client(owner)

    response = client.post(
        f"/api/events/{event.id}/reopen/",
        {"responseDeadline": (timezone.now() + timedelta(days=3)).isoformat()},
        format="json",
    )
    assert response.status_code == status.HTTP_200_OK

    poll_response = APIClient().get(_poll_url(event.id))

    assert poll_response.status_code == status.HTTP_200_OK
    body = poll_response.json()
    assert body["status"] == "active"
    assert body["displayStatus"] == "voting_open"


def test_poll_event_updated_at_changes_on_finalize_cancel_reopen():
    """⑥c code-review 抓到:finalize/cancel/reopen 都是走 CAS `.update()`,
    不是 `.save()`,`auto_now` 對 `.update()` 不生效——若沒有明確蓋章,
    `eventUpdatedAt` 在這三個動作後都不會變,輪詢端點就偵測不到活動本身
    (非投票/留言)的變化。"""
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    client = _auth_client(owner)

    before_finalize = APIClient().get(_poll_url(event.id)).json()["eventUpdatedAt"]

    finalize_response = client.post(
        f"/api/events/{event.id}/finalize/",
        {"finalSlotId": str(slot.id)},
        format="json",
    )
    assert finalize_response.status_code == status.HTTP_200_OK
    after_finalize = APIClient().get(_poll_url(event.id)).json()["eventUpdatedAt"]
    assert after_finalize != before_finalize

    reopen_response = client.post(
        f"/api/events/{event.id}/reopen/",
        {"responseDeadline": (timezone.now() + timedelta(days=3)).isoformat()},
        format="json",
    )
    assert reopen_response.status_code == status.HTTP_200_OK
    after_reopen = APIClient().get(_poll_url(event.id)).json()["eventUpdatedAt"]
    assert after_reopen != after_finalize

    cancel_response = client.post(f"/api/events/{event.id}/cancel/", {}, format="json")
    assert cancel_response.status_code == status.HTTP_200_OK
    after_cancel = APIClient().get(_poll_url(event.id)).json()["eventUpdatedAt"]
    assert after_cancel != after_reopen


def test_poll_event_updated_at_detects_reopen_then_refinalize_with_unchanged_counts():
    """⑥d codex review finding:前端輪詢期間主揪 reopen 後又重新 finalize、
    改選另一個最終時段,若 responseCount/commentCount/status/displayStatus
    跟上一輪完全相同(沒有新投票也沒有新留言),沒有 eventUpdatedAt 的話前端
    會誤判「沒有變化」而不去重新取得完整活動內容,继续顯示舊的
    finalSlotId。"""
    owner = _create_user()
    event = _create_event(owner)
    slot_1 = event.slots.first()
    slot_2 = _add_slot(event, date="2026-10-03")
    client = _auth_client(owner)

    client.post(
        f"/api/events/{event.id}/finalize/",
        {"finalSlotId": str(slot_1.id)},
        format="json",
    )
    first_poll = APIClient().get(_poll_url(event.id)).json()

    client.post(
        f"/api/events/{event.id}/reopen/",
        {"responseDeadline": (timezone.now() + timedelta(days=3)).isoformat()},
        format="json",
    )
    client.post(
        f"/api/events/{event.id}/finalize/",
        {"finalSlotId": str(slot_2.id)},
        format="json",
    )
    second_poll = APIClient().get(_poll_url(event.id)).json()

    assert second_poll["status"] == first_poll["status"] == "finalized"
    assert second_poll["displayStatus"] == first_poll["displayStatus"]
    assert second_poll["responseCount"] == first_poll["responseCount"] == 0
    assert second_poll["commentCount"] == first_poll["commentCount"] == 0
    assert second_poll["eventUpdatedAt"] != first_poll["eventUpdatedAt"]


def test_poll_link_expired_returns_410():
    """⑦ 連結已失效（取消超過 7 天）→ 410 LINK_EXPIRED。"""
    owner = _create_user()
    event = _create_event(
        owner,
        status=Event.Status.CANCELLED,
        cancelled_at=timezone.now() - timedelta(days=8),
    )
    client = APIClient()

    response = client.get(_poll_url(event.id))

    assert response.status_code == status.HTTP_410_GONE
    assert response.json()["code"] == "LINK_EXPIRED"


def test_poll_nonexistent_event_returns_404():
    """⑧ 活動不存在 → 404 EVENT_NOT_FOUND。"""
    client = APIClient()

    response = client.get(_poll_url(generate_short_id()))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["code"] == "EVENT_NOT_FOUND"


def test_poll_does_not_require_authentication():
    """⑨ 未登入（不帶 token）也能成功查詢（公開端點）。"""
    owner = _create_user()
    event = _create_event(owner)

    response = APIClient().get(_poll_url(event.id))

    assert response.status_code == status.HTTP_200_OK
