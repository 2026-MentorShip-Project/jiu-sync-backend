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
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User
from apps.events.ids import generate_short_id
from apps.events.models import (
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
    """② 活動無任何投票 → responses 為空陣列(既有行為維持)。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()

    response = client.get(_detail_url(event.id))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["responses"] == []


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
    # 可直接拿來渲染,不用另外再打一次 GET(design.md D16)。
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
        "responses",
    }
    assert body["id"] == str(event.id)
    assert len(body["responses"]) == 1
    new_response_body = body["responses"][0]
    assert RESPONSE_SHORT_ID_RE.match(new_response_body["id"])

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
    原投票內容(nickname/email/slotAvailabilities,供前端預填);DB 新增一筆
    token 紀錄,token_hash 不等於明碼 accessToken。"""
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
        "nickname",
        "email",
        "slotAvailabilities",
    }
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
