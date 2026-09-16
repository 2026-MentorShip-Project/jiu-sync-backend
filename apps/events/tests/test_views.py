import uuid
from datetime import timedelta

import pytest
from django.conf import settings
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User
from apps.events.models import Event, Slot

pytestmark = pytest.mark.django_db

EVENTS_URL = "/api/events/"


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
    """③ responseDeadline 等於或早於送出當下時間 → 400,不建立任何資料。"""
    user = _create_user()
    client = _auth_client(user)
    now = timezone.now()

    for deadline in (now, now - timedelta(hours=1)):
        response = client.post(
            EVENTS_URL, _valid_payload(responseDeadline=deadline.isoformat()), format="json"
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    assert Event.objects.count() == 0


def test_slots_count_zero_or_over_20_returns_400():
    """④ slots 為 0 筆或超過 20 筆 → 400,不建立任何資料。"""
    user = _create_user()
    client = _auth_client(user)
    too_many_slots = [{"date": f"2026-10-{day:02d}"} for day in range(1, 22)]

    for slots in ([], too_many_slots):
        response = client.post(EVENTS_URL, _valid_payload(slots=slots), format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    assert Event.objects.count() == 0


def test_title_over_30_chars_returns_400():
    """⑤ title 超過 30 字元 → 400。"""
    user = _create_user()
    client = _auth_client(user)

    response = client.post(EVENTS_URL, _valid_payload(title="揪" * 31), format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert Event.objects.count() == 0


def test_host_nickname_weighted_length_exactly_40_is_allowed():
    """⑥ 邊界:hostNickname 加權長度剛好等於 40(CJK 字元計 2)→ 通過。"""
    user = _create_user()
    client = _auth_client(user)
    nickname = "揪" * 20  # 20 CJK chars * weight 2 = 40

    response = client.post(EVENTS_URL, _valid_payload(hostNickname=nickname), format="json")

    assert response.status_code == status.HTTP_201_CREATED


def test_host_nickname_weighted_length_over_40_returns_400():
    """⑥ hostNickname 加權長度超過 40(CJK 字元計 2、其餘計 1)→ 400。"""
    user = _create_user()
    client = _auth_client(user)
    nickname = "揪" * 21  # 21 CJK chars * weight 2 = 42

    response = client.post(EVENTS_URL, _valid_payload(hostNickname=nickname), format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
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
    """⑨ slot label 超過 100 字元 → 400 驗證錯誤(不是 DB DataError 500),不建立任何資料。
    修正 Codex review 項目 B:SlotCreateSerializer 是 plain Serializer,不會自動繼承
    Slot model 的 max_length=100。"""
    user = _create_user()
    client = _auth_client(user)
    payload = _valid_payload(slots=[{"date": "2026-10-01", "label": "a" * 101}])

    response = client.post(EVENTS_URL, payload, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert Event.objects.count() == 0


@override_settings(FRONTEND_BASE_URL="https://example.com/")
def test_share_url_has_no_double_slash_when_frontend_base_url_has_trailing_slash():
    """⑩ FRONTEND_BASE_URL 帶結尾斜線(例如 "https://example.com/")→ 組出的
    shareUrl 不應出現雙斜線。修正 Codex review 項目 D。"""
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


def test_nonexistent_event_id_returns_404_with_api_error_shape():
    """④ 請求不存在的 id → 404,body 符合 api-error-format 的 {message, code} 形狀。"""
    client = APIClient()

    response = client.get(_detail_url(uuid.uuid4()))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    body = response.json()
    assert set(body.keys()) == {"message", "code"}


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
    isOwner=false,hostEmail=null。修正 Codex review 項目 A:AllowAny 只跳過權限
    檢查,不跳過認證本身——壞 token 不該比完全不帶 token 更嚴格。"""
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


def test_event_detail_responses_field_is_always_empty_list():
    """⑥ 回應的 responses 欄位固定為空陣列 []。"""
    owner = _create_user()
    event = _create_event(owner)
    client = APIClient()

    response = client.get(_detail_url(event.id))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["responses"] == []


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
    """④ 缺少 owner=me 查詢參數 → 400 驗證錯誤(專案慣例:走
    serializers.ValidationError,由 custom_exception_handler 統一包裝)。"""
    user = _create_user()
    client = _auth_client(user)

    response = client.get(EVENTS_URL)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
