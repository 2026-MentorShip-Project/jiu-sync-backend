"""`POST /api/events/{id}/restaurant-recommendations/` 主流程(tasks.md 2.1)。

Seam:HTTP(DRF `APIClient`)。引擎以 `fake_engine` fixture 替換 view 用的
`get_engine()`,可控制回傳結果或丟出指定例外,並記錄呼叫次數——CI 永遠不打
真的 Perplexity。

見 openspec/changes/add-ai-restaurant-recommendation/specs/restaurant-recommendations/spec.md
與 design.md D2/D4/D7/D9/D11。併發與額度上限(403/409 in-progress)在 section 3。
"""

import json
import logging
from datetime import time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User
from apps.events.models import (
    Event,
    ParticipantResponse,
    ParticipantResponseSlotAvailability,
    Slot,
)
from apps.recommendations.engines import (
    NoUsableResults,
    RecommendationResult,
    UpstreamHTTPError,
    UpstreamInvalidResponse,
    UpstreamTimeout,
)
from apps.recommendations.logging import JsonFormatter
from apps.recommendations.models import RestaurantRecommendationRequest

pytestmark = pytest.mark.django_db

TAIPEI = ZoneInfo("Asia/Taipei")
QUOTA_URL = "/api/me/ai-recommendation-quota/"
Status = RestaurantRecommendationRequest.Status

MARKER = "ZZ-SECRET-MARKER-7f3a"


def _url(event_id):
    return f"/api/events/{event_id}/restaurant-recommendations/"


def _today_taipei():
    return timezone.now().astimezone(TAIPEI).date()


def _create_user(email="host@example.com", google_sub="sub-1"):
    return User.objects.create_user(
        email=email, google_sub=google_sub, display_name="Host", avatar_url=""
    )


def _auth_client(user):
    access_token = str(RefreshToken.for_user(user).access_token)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")
    return client


def _create_finalized_event(
    owner,
    *,
    location="台北車站",
    slot_date=None,
    slot_time=time(18, 30),
    slot_label=None,
    finalized_at=None,
):
    now = timezone.now()
    event = Event.objects.create(
        owner=owner,
        title="聚餐",
        host_nickname="小明",
        mode=Event.Mode.TIME_SLOTS,
        response_deadline=now - timedelta(hours=1),
        location=location,
    )
    slot = Slot.objects.create(
        event=event,
        date=slot_date or (_today_taipei() + timedelta(days=3)),
        time=slot_time,
        label=slot_label,
    )
    event.status = Event.Status.FINALIZED
    event.final_slot = slot
    event.finalized_at = finalized_at or now
    event.save()
    return event


def _add_response(event, nickname, availability, *, slot=None, deleted=False):
    response = ParticipantResponse.objects.create(
        event=event,
        nickname=nickname,
        phone_last_three_hash="x",
        deleted_at=timezone.now() if deleted else None,
    )
    ParticipantResponseSlotAvailability.objects.create(
        response=response, slot=slot or event.final_slot, availability=availability
    )
    return response


def _default_result():
    return RecommendationResult(
        restaurants=[
            {
                "id": "r1",
                "name": "好吃小館",
                "address": "台北市中正區忠孝西路一段 1 號",
                "phone": None,
                "rating": 4.5,
                "reviewCount": 120,
                "openingHours": "11:00-21:00",
                "priceRange": "$$",
                "avgPricePerPerson": {"min": 300, "max": 500},
                "cuisineType": "台菜",
                "distanceInfo": {"transitPoint": "台北車站", "walkMinutes": 5},
                "recommendReason": "適合聚餐",
                "sourceUrl": "https://example.com/a",
            },
            {
                "id": "r2",
                "name": "第二間",
                "address": "台北市中正區館前路 2 號",
                "phone": None,
                "rating": None,
                "reviewCount": None,
                "openingHours": None,
                "priceRange": None,
                "avgPricePerPerson": None,
                "cuisineType": None,
                "distanceInfo": None,
                "recommendReason": None,
                "sourceUrl": None,
            },
        ],
        notes="部分營業時間無法確認",
        usage={"input_tokens": 1000, "output_tokens": 234, "total_tokens": 1234,
               "cost": {"total_cost": 0.0123}},
        model="openai/gpt-6-luna",
    )


class FakeEngine:
    """可控制的假引擎:`result` 為成功回傳值,`error` 非 None 時丟出它。"""

    model_name = "preset:low"

    def __init__(self):
        self.available = True
        self.result = _default_result()
        self.error = None
        self.calls = []

    def is_available(self):
        return self.available

    def recommend(self, context):
        self.calls.append(context)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def fake_engine(monkeypatch):
    engine = FakeEngine()
    monkeypatch.setattr("apps.recommendations.views.get_engine", lambda: engine)
    return engine


def _used(client):
    return client.get(QUOTA_URL).json()["used"]


# ---------------------------------------------------------------------------
# ① 成功
# ---------------------------------------------------------------------------


def test_success_returns_201_with_restaurants_preferences_and_updated_quota(fake_engine):
    user = _create_user()
    event = _create_finalized_event(user)
    client = _auth_client(user)
    before = timezone.now()

    response = client.post(_url(event.id), {"relationship": "同事"}, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert set(body) == {"id", "restaurants", "notes", "resolvedPreferences", "quota"}
    assert [r["id"] for r in body["restaurants"]] == ["r1", "r2"]
    assert body["restaurants"][0]["name"] == "好吃小館"
    assert body["notes"] == "部分營業時間無法確認"
    assert body["resolvedPreferences"]["relationship"] == "同事"
    assert body["resolvedPreferences"]["location"] == "台北車站"
    assert body["quota"]["used"] == 1
    assert body["quota"]["remaining"] == 19
    assert set(body["quota"]) == {"period", "limit", "used", "remaining", "available", "resetsAt"}
    assert len(fake_engine.calls) == 1

    record = RestaurantRecommendationRequest.objects.get()
    assert str(record.id) == body["id"]
    assert record.status == Status.SUCCEEDED
    assert record.user_id == user.id
    assert record.event_id == event.id
    assert record.preferences == body["resolvedPreferences"]
    assert record.result == {"restaurants": body["restaurants"], "notes": body["notes"]}
    assert record.usage == _default_result().usage
    assert record.model == "openai/gpt-6-luna"
    assert isinstance(record.latency_ms, int) and record.latency_ms >= 0
    assert record.completed_at is not None
    assert record.created_at >= before
    assert record.quota_period == record.created_at.astimezone(TAIPEI).strftime("%Y-%m")
    assert record.error_code is None


# ---------------------------------------------------------------------------
# ②–⑧ 前置檢查
# ---------------------------------------------------------------------------


def test_unauthenticated_returns_401(fake_engine):
    event = _create_finalized_event(_create_user())

    response = APIClient().post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert fake_engine.calls == []


def test_missing_event_returns_404_event_not_found(fake_engine):
    client = _auth_client(_create_user())

    response = client.post(_url("Zzzzzzzz"), {}, format="json")

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["code"] == "EVENT_NOT_FOUND"


def test_non_owner_gets_403_and_engine_not_called(fake_engine):
    owner = _create_user()
    other = _create_user(email="other@example.com", google_sub="sub-2")
    event = _create_finalized_event(owner)

    response = _auth_client(other).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert fake_engine.calls == []
    assert RestaurantRecommendationRequest.objects.count() == 0


def _make_voting_open(event):
    event.status = Event.Status.ACTIVE
    event.final_slot = None
    event.finalized_at = None
    event.response_deadline = timezone.now() + timedelta(days=1)


def _make_voting_closed(event):
    event.status = Event.Status.ACTIVE
    event.final_slot = None
    event.finalized_at = None
    event.response_deadline = timezone.now() - timedelta(hours=1)


def _make_cancelled(event):
    event.status = Event.Status.CANCELLED
    event.final_slot = None
    event.finalized_at = None
    event.cancelled_at = timezone.now()


@pytest.mark.parametrize("mutate", [_make_voting_open, _make_voting_closed, _make_cancelled])
def test_not_finalized_event_returns_409(fake_engine, mutate):
    user = _create_user()
    event = _create_finalized_event(user)
    mutate(event)
    event.save()

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "EVENT_NOT_FINALIZED"
    assert fake_engine.calls == []
    assert RestaurantRecommendationRequest.objects.count() == 0


def test_past_event_returns_409_event_already_past(fake_engine):
    user = _create_user()
    event = _create_finalized_event(user, slot_date=_today_taipei() - timedelta(days=1))

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "EVENT_ALREADY_PAST"
    assert fake_engine.calls == []


def test_event_today_is_not_past(fake_engine):
    """邊界:定案日期 = 今天(台灣時間)仍可推薦。"""
    user = _create_user()
    event = _create_finalized_event(user, slot_date=_today_taipei())

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_201_CREATED


def test_link_expired_returns_410(fake_engine):
    user = _create_user()
    event = _create_finalized_event(user, finalized_at=timezone.now() - timedelta(days=8))

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == 410
    assert response.json()["code"] == "LINK_EXPIRED"
    assert fake_engine.calls == []


def test_engine_unavailable_returns_503_without_record(fake_engine):
    fake_engine.available = False
    user = _create_user()
    event = _create_finalized_event(user)

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json()["code"] == "AI_RECOMMENDATION_UNAVAILABLE"
    assert fake_engine.calls == []
    assert RestaurantRecommendationRequest.objects.count() == 0


# ---------------------------------------------------------------------------
# ⑨ 欄位驗證
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"relationship": "路人"}, "relationship"),
        ({"budget": "100"}, "budget"),
        ({"partySize": "9 人以上(多人)"}, "partySize"),  # 半形括號不是允許值
        ({"situational": ["可久坐", "可久坐"]}, "situational"),
        ({"situational": ["可以唱歌"]}, "situational"),
        ({"dietary": {"spice": "小辣"}}, "dietary.spice"),
        ({"dietary": {"vegetarian": "maybe"}}, "dietary.vegetarian"),
        ({"dietary": {"cuisines": ["a", "b", "c", "d", "e", "f"]}}, "dietary.cuisines"),
        ({"dietary": {"cuisines": ["泰" * 21]}}, "dietary.cuisines"),
        ({"dietary": {"restrictions": ["不" * 31]}}, "dietary.restrictions"),
        ({"dietary": {"restrictions": ["1", "2", "3", "4", "5", "6"]}}, "dietary.restrictions"),
        ({"customPrompt": "字" * 201}, "customPrompt"),
        ({"location": "地" * 101}, "location"),
    ],
)
def test_invalid_field_returns_400_listing_field(fake_engine, body, field):
    user = _create_user()
    event = _create_finalized_event(user)

    response = _auth_client(user).post(_url(event.id), body, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    errors = response.json()["errors"]
    assert [e["field"].split("[")[0] for e in errors] == [field]
    assert all(e["code"] for e in errors)
    assert fake_engine.calls == []
    assert RestaurantRecommendationRequest.objects.count() == 0


def test_multiple_invalid_fields_are_all_listed(fake_engine):
    user = _create_user()
    event = _create_finalized_event(user)
    body = {
        "relationship": "路人",
        "situational": ["停車位", "停車位"],
        "dietary": {"cuisines": ["a"] * 6, "restrictions": ["不" * 31]},
        "customPrompt": "字" * 201,
    }

    response = _auth_client(user).post(_url(event.id), body, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    fields = {e["field"].split("[")[0] for e in response.json()["errors"]}
    assert fields == {
        "relationship",
        "situational",
        "dietary.cuisines",
        "dietary.restrictions",
        "customPrompt",
    }
    assert RestaurantRecommendationRequest.objects.count() == 0


def test_boundary_lengths_are_accepted(fake_engine):
    user = _create_user()
    event = _create_finalized_event(user)
    body = {
        "location": "地" * 100,
        "situational": ["可久坐", "有插座", "停車位", "親子友善", "無障礙"],
        "dietary": {
            "vegetarian": True,
            "spice": "不吃辣",
            "cuisines": ["泰" * 20, "火鍋", "c", "d", "e"],
            "restrictions": ["不" * 30],
        },
        "customPrompt": "字" * 200,
        "partySize": "9 人以上（多人）",
        "budget": "1000 以上",
    }

    response = _auth_client(user).post(_url(event.id), body, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    resolved = response.json()["resolvedPreferences"]
    assert resolved["dietary"]["cuisines"][0] == "泰" * 20
    assert resolved["partySize"] == "9 人以上（多人）"


def test_whitespace_only_strings_are_treated_as_unset(fake_engine):
    user = _create_user()
    event = _create_finalized_event(user, location="台北車站")
    body = {
        "location": "   ",
        "relationship": "  ",
        "customPrompt": " \n ",
        "dietary": {"cuisines": ["  ", " 泰式 "], "restrictions": ["   "]},
    }

    response = _auth_client(user).post(_url(event.id), body, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    resolved = response.json()["resolvedPreferences"]
    assert resolved["location"] == "台北車站"
    assert resolved["locationSource"] == "event"
    assert resolved["relationship"] is None
    assert resolved["customPrompt"] is None
    assert resolved["dietary"]["cuisines"] == ["泰式"]
    assert resolved["dietary"]["restrictions"] == []


@pytest.mark.parametrize("field", ["cuisines", "restrictions"])
@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_list_items_are_dropped_before_max_items_check(fake_engine, field, blank):
    user = _create_user()
    event = _create_finalized_event(user)
    items = ["a", "b", blank, "c", "d", "e"]

    response = _auth_client(user).post(
        _url(event.id), {"dietary": {field: items}}, format="json"
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["resolvedPreferences"]["dietary"][field] == ["a", "b", "c", "d", "e"]
    assert fake_engine.calls[0].preferences["dietary"][field] == ["a", "b", "c", "d", "e"]


def test_situational_blank_items_are_dropped_before_duplicate_check(fake_engine):
    user = _create_user()
    event = _create_finalized_event(user)
    body = {"situational": ["可久坐", " ", "有插座", "停車位", "親子友善", "無障礙", ""]}

    response = _auth_client(user).post(_url(event.id), body, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["resolvedPreferences"]["situational"] == [
        "可久坐", "有插座", "停車位", "親子友善", "無障礙"
    ]


def test_custom_cuisines_are_accepted(fake_engine):
    user = _create_user()
    event = _create_finalized_event(user)

    response = _auth_client(user).post(
        _url(event.id), {"dietary": {"cuisines": ["泰式", "火鍋"]}}, format="json"
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["resolvedPreferences"]["dietary"]["cuisines"] == ["泰式", "火鍋"]
    assert fake_engine.calls[0].preferences["dietary"]["cuisines"] == ["泰式", "火鍋"]


# ---------------------------------------------------------------------------
# ⑩–⑬ 條件解析(D9)
# ---------------------------------------------------------------------------


def test_empty_body_uses_event_location(fake_engine):
    user = _create_user()
    event = _create_finalized_event(user, location="台北車站")

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    resolved = response.json()["resolvedPreferences"]
    assert resolved["location"] == "台北車站"
    assert resolved["locationSource"] == "event"
    assert fake_engine.calls[0].preferences == resolved


def test_request_location_takes_priority(fake_engine):
    user = _create_user()
    event = _create_finalized_event(user, location="台北車站")

    response = _auth_client(user).post(_url(event.id), {"location": " 中山站 "}, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    resolved = response.json()["resolvedPreferences"]
    assert resolved["location"] == "中山站"
    assert resolved["locationSource"] == "request"


@pytest.mark.parametrize("event_location", [None, "", "   "])
def test_no_location_anywhere_returns_400_location_required(fake_engine, event_location):
    user = _create_user()
    event = _create_finalized_event(user, location=event_location)

    response = _auth_client(user).post(_url(event.id), {"location": ""}, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "LOCATION_REQUIRED"
    assert fake_engine.calls == []
    assert RestaurantRecommendationRequest.objects.count() == 0


def test_party_size_defaults_to_available_non_deleted_responses(fake_engine):
    user = _create_user()
    event = _create_finalized_event(user)
    other_slot = Slot.objects.create(event=event, date=event.final_slot.date)
    _add_response(event, "a", "available")
    _add_response(event, "b", "available")
    _add_response(event, "c", "if_needed")
    _add_response(event, "d", "unavailable")
    _add_response(event, "e", "available", deleted=True)
    _add_response(event, "f", "available", slot=other_slot)

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    resolved = response.json()["resolvedPreferences"]
    # 2.3 商業邏輯變更:partySize 一律為人數規格字串,實際人數改由 attendeeCount 提供。
    assert resolved["attendeeCount"] == 2
    assert resolved["partySize"] == "2 人"
    assert fake_engine.calls[0].preferences["partySize"] == "2 人"


@pytest.mark.parametrize(
    ("attendees", "expected"),
    [(1, "2 人"), (3, "3-4 人"), (5, "5-8 人"), (9, "9 人以上（多人）"), (20, "20 人以上（團體）")],
)
def test_party_size_is_derived_from_attendee_count_when_not_chosen(
    fake_engine, attendees, expected
):
    user = _create_user()
    event = _create_finalized_event(user)
    for i in range(attendees):
        _add_response(event, f"p{i}", "available")

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    resolved = response.json()["resolvedPreferences"]
    assert resolved["attendeeCount"] == attendees
    assert resolved["partySize"] == expected


def test_no_available_responses_gives_null_party_size_and_engine_gets_no_party_size(
    fake_engine,
):
    user = _create_user()
    event = _create_finalized_event(user)
    _add_response(event, "a", "if_needed")
    _add_response(event, "b", "available", deleted=True)

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    resolved = response.json()["resolvedPreferences"]
    assert resolved["attendeeCount"] == 0
    assert resolved["partySize"] is None
    sent = fake_engine.calls[0].preferences
    assert sent["partySize"] is None
    assert sent["attendeeCount"] == 0
    record = RestaurantRecommendationRequest.objects.get()
    assert record.preferences["partySize"] is None
    assert record.preferences["attendeeCount"] == 0


def test_party_size_from_request_takes_priority(fake_engine):
    user = _create_user()
    event = _create_finalized_event(user)
    _add_response(event, "a", "available")

    response = _auth_client(user).post(_url(event.id), {"partySize": "5-8 人"}, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    resolved = response.json()["resolvedPreferences"]
    assert resolved["partySize"] == "5-8 人"
    assert resolved["attendeeCount"] == 1
    assert fake_engine.calls[0].preferences["partySize"] == "5-8 人"


def test_chosen_party_size_is_kept_even_with_zero_attendees(fake_engine):
    user = _create_user()
    event = _create_finalized_event(user)

    response = _auth_client(user).post(_url(event.id), {"partySize": "2 人"}, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    resolved = response.json()["resolvedPreferences"]
    assert resolved["partySize"] == "2 人"
    assert resolved["attendeeCount"] == 0


def test_meal_time_uses_final_slot_date_and_time_or_label(fake_engine):
    user = _create_user()
    date = _today_taipei() + timedelta(days=2)
    with_time = _create_finalized_event(user, slot_date=date, slot_time=time(18, 30))
    with_label = _create_finalized_event(user, slot_date=date, slot_time=None, slot_label="晚上")
    date_only = _create_finalized_event(user, slot_date=date, slot_time=None)
    client = _auth_client(user)

    resolved = [
        client.post(_url(e.id), {}, format="json").json()["resolvedPreferences"]
        for e in (with_time, with_label, date_only)
    ]

    assert [(r["mealDate"], r["mealTime"]) for r in resolved] == [
        (date.isoformat(), "18:30"),
        (date.isoformat(), "晚上"),
        (date.isoformat(), None),
    ]


# ---------------------------------------------------------------------------
# ⑭–⑰ 上游失敗 → 釋放預留
# ---------------------------------------------------------------------------


def test_upstream_timeout_returns_504_and_does_not_count(fake_engine):
    fake_engine.error = UpstreamTimeout("read timeout")
    user = _create_user()
    event = _create_finalized_event(user)
    client = _auth_client(user)

    response = client.post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_504_GATEWAY_TIMEOUT
    assert response.json()["code"] == "AI_RECOMMENDATION_UPSTREAM_TIMEOUT"
    assert "notes" not in response.json()
    record = RestaurantRecommendationRequest.objects.get()
    assert record.status == Status.FAILED
    assert record.error_code == "UPSTREAM_TIMEOUT"
    assert record.completed_at is not None
    assert isinstance(record.latency_ms, int)
    assert _used(client) == 0


@pytest.mark.parametrize(
    ("error", "error_code"),
    [
        (UpstreamHTTPError(429, raw_detail='{"error":{"message":"rate"}}'), "UPSTREAM_HTTP_ERROR"),
        (UpstreamInvalidResponse("JSON decode failed", raw_detail="not json"),
         "UPSTREAM_INVALID_RESPONSE"),
    ],
)
def test_upstream_failures_return_502_without_notes(fake_engine, error, error_code):
    fake_engine.error = error
    user = _create_user()
    event = _create_finalized_event(user)
    client = _auth_client(user)

    response = client.post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.json()["code"] == "AI_RECOMMENDATION_UPSTREAM_FAILED"
    assert "notes" not in response.json()
    record = RestaurantRecommendationRequest.objects.get()
    assert record.status == Status.FAILED
    assert record.error_code == error_code
    assert record.error_detail == error.raw_detail
    assert _used(client) == 0


@pytest.mark.parametrize("notes", ["附近沒有符合素食條件的店", None])
def test_no_usable_results_returns_502_with_notes(fake_engine, notes):
    fake_engine.error = NoUsableResults(notes)
    user = _create_user()
    event = _create_finalized_event(user)
    client = _auth_client(user)

    response = client.post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    body = response.json()
    assert body["code"] == "AI_RECOMMENDATION_UPSTREAM_FAILED"
    assert "notes" in body
    assert body["notes"] == notes
    record = RestaurantRecommendationRequest.objects.get()
    assert record.status == Status.FAILED
    assert record.error_code == "NO_USABLE_RESULTS"
    assert _used(client) == 0


def test_error_detail_is_truncated_to_2000_chars(fake_engine):
    fake_engine.error = UpstreamInvalidResponse("structure mismatch", raw_detail="x" * 2500)
    user = _create_user()
    event = _create_finalized_event(user)

    _auth_client(user).post(_url(event.id), {}, format="json")

    assert RestaurantRecommendationRequest.objects.get().error_detail == "x" * 2000


def test_unexpected_error_returns_500_and_marks_failed(fake_engine):
    fake_engine.error = RuntimeError("boom")
    user = _create_user()
    event = _create_finalized_event(user)
    client = _auth_client(user)
    client.raise_request_exception = False

    response = client.post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    record = RestaurantRecommendationRequest.objects.get()
    assert record.status == Status.FAILED
    assert record.error_code == "UNEXPECTED_ERROR"
    assert record.completed_at is not None
    assert not RestaurantRecommendationRequest.objects.filter(status=Status.PENDING).exists()
    assert _used(client) == 0


def test_retry_after_failure_succeeds(fake_engine):
    fake_engine.error = UpstreamTimeout("read timeout")
    user = _create_user()
    event = _create_finalized_event(user)
    client = _auth_client(user)
    first = client.post(_url(event.id), {}, format="json")
    fake_engine.error = None

    second = client.post(_url(event.id), {}, format="json")

    assert first.status_code == status.HTTP_504_GATEWAY_TIMEOUT
    assert second.status_code == status.HTTP_201_CREATED
    assert second.json()["quota"]["used"] == 1
    statuses = sorted(RestaurantRecommendationRequest.objects.values_list("status", flat=True))
    assert statuses == [Status.FAILED, Status.SUCCEEDED]


# ---------------------------------------------------------------------------
# ⑱ 重新推薦也計次
# ---------------------------------------------------------------------------


def test_re_recommend_counts_again_and_keeps_previous_result(fake_engine):
    user = _create_user()
    event = _create_finalized_event(user)
    client = _auth_client(user)
    first = client.post(_url(event.id), {}, format="json")
    first_record = RestaurantRecommendationRequest.objects.get()
    first_result = first_record.result
    fake_engine.result = RecommendationResult(
        restaurants=[{"id": "r1", "name": "新店", "address": "新地址"}],
        notes=None,
        usage=None,
        model="openai/gpt-6-luna",
    )

    second = client.post(_url(event.id), {}, format="json")

    assert first.status_code == second.status_code == status.HTTP_201_CREATED
    assert first.json()["quota"]["used"] == 1
    assert second.json()["quota"]["used"] == 2
    assert second.json()["id"] != first.json()["id"]
    assert RestaurantRecommendationRequest.objects.filter(status=Status.SUCCEEDED).count() == 2
    first_record.refresh_from_db()
    assert first_record.result == first_result


# ---------------------------------------------------------------------------
# ⑲ 結構化 log
# ---------------------------------------------------------------------------


def _events(caplog, name):
    return [r for r in caplog.records if getattr(r, "event", None) == name]


def test_success_logs_ai_rec_succeeded_with_numeric_fields(fake_engine, caplog):
    caplog.set_level(logging.INFO)
    user = _create_user()
    event = _create_finalized_event(user)

    body = _auth_client(user).post(_url(event.id), {}, format="json").json()

    records = _events(caplog, "ai_rec.succeeded")
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    line = json.loads(JsonFormatter().format(records[0]))
    assert line["request_id"] == body["id"]
    assert line["user_id"] == str(user.id)
    assert line["event_id"] == event.id
    assert isinstance(line["latency_ms"], int)
    assert line["restaurant_count"] == 2
    assert line["total_tokens"] == 1234
    assert line["cost_usd"] == 0.0123
    assert line["model"] == "openai/gpt-6-luna"


def test_success_log_with_missing_usage_has_null_cost(fake_engine, caplog):
    caplog.set_level(logging.INFO)
    fake_engine.result = RecommendationResult(
        restaurants=[{"id": "r1", "name": "n", "address": "a"}], notes=None, usage=None,
        model="m",
    )
    user = _create_user()
    event = _create_finalized_event(user)

    _auth_client(user).post(_url(event.id), {}, format="json")

    line = json.loads(JsonFormatter().format(_events(caplog, "ai_rec.succeeded")[0]))
    assert line["cost_usd"] is None
    assert line["total_tokens"] is None


def test_timeout_logs_ai_rec_failed_with_exception_info(fake_engine, caplog):
    caplog.set_level(logging.INFO)
    fake_engine.error = UpstreamTimeout("read timeout")
    user = _create_user()
    event = _create_finalized_event(user)

    _auth_client(user).post(_url(event.id), {}, format="json")

    records = _events(caplog, "ai_rec.failed")
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    line = json.loads(JsonFormatter().format(records[0]))
    assert line["error_code"] == "UPSTREAM_TIMEOUT"
    assert line["exc_type"] == "UpstreamTimeout"
    assert isinstance(line["latency_ms"], int)
    assert line["upstream_status"] is None
    assert line["request_id"] == str(RestaurantRecommendationRequest.objects.get().id)


def test_http_error_log_has_upstream_status(fake_engine, caplog):
    caplog.set_level(logging.INFO)
    fake_engine.error = UpstreamHTTPError(503)
    user = _create_user()
    event = _create_finalized_event(user)

    _auth_client(user).post(_url(event.id), {}, format="json")

    line = json.loads(JsonFormatter().format(_events(caplog, "ai_rec.failed")[0]))
    assert line["upstream_status"] == 503
    assert line["error_code"] == "UPSTREAM_HTTP_ERROR"


def test_unexpected_error_logs_ai_rec_failed_at_error_level(fake_engine, caplog):
    caplog.set_level(logging.INFO)
    fake_engine.error = RuntimeError("boom")
    user = _create_user()
    event = _create_finalized_event(user)
    client = _auth_client(user)
    client.raise_request_exception = False

    client.post(_url(event.id), {}, format="json")

    records = _events(caplog, "ai_rec.failed")
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    line = json.loads(JsonFormatter().format(records[0]))
    assert line["error_code"] == "UNEXPECTED_ERROR"
    assert line["exc_type"] == "RuntimeError"


def test_unavailable_logs_ai_rec_unavailable(fake_engine, caplog):
    caplog.set_level(logging.INFO)
    fake_engine.available = False
    user = _create_user()
    event = _create_finalized_event(user)

    _auth_client(user).post(_url(event.id), {}, format="json")

    records = _events(caplog, "ai_rec.unavailable")
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    line = json.loads(JsonFormatter().format(records[0]))
    assert line["user_id"] == str(user.id)
    assert line["event_id"] == event.id


@pytest.mark.parametrize(
    "error",
    [
        None,
        UpstreamTimeout("read timeout"),
        UpstreamHTTPError(500, raw_detail=f"upstream body {MARKER}"),
        UpstreamInvalidResponse("JSON decode failed at char 3", raw_detail=f"bad {MARKER}"),
        NoUsableResults(f"notes {MARKER}", raw_detail=f"raw {MARKER}"),
    ],
    ids=["success", "timeout", "http", "invalid", "no-results"],
)
def test_logs_never_contain_user_text_or_raw_upstream(fake_engine, caplog, capsys, error):
    caplog.set_level(logging.DEBUG)
    fake_engine.error = error
    user = _create_user()
    event = _create_finalized_event(user)
    body = {
        "customPrompt": f"想吃 {MARKER}",
        "dietary": {"cuisines": [f"c{MARKER}"[:20]], "restrictions": [f"r {MARKER}"]},
    }

    _auth_client(user).post(_url(event.id), body, format="json")

    assert any(
        str(getattr(r, "event", "")).startswith("ai_rec.") for r in caplog.records
    ), "預期至少有一筆推薦事件 log"
    plain = logging.Formatter()
    json_formatter = JsonFormatter()
    for record in caplog.records:
        assert MARKER[:10] not in plain.format(record)
        assert MARKER[:10] not in json_formatter.format(record)
        assert MARKER[:10] not in repr(record.__dict__)
    captured = capsys.readouterr()
    assert MARKER[:10] not in captured.out
    assert MARKER[:10] not in captured.err
    if error is not None and error.raw_detail:
        assert MARKER in RestaurantRecommendationRequest.objects.get().error_detail


def test_non_object_dietary_is_reported_as_dietary_field(fake_engine):
    user = _create_user()
    event = _create_finalized_event(user)

    response = _auth_client(user).post(_url(event.id), {"dietary": "素食"}, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    errors = response.json()["errors"]
    assert [(e["field"], e["code"]) for e in errors] == [("dietary", "DIETARY_INVALID")]


def test_non_object_body_returns_400(fake_engine):
    user = _create_user()
    event = _create_finalized_event(user)

    response = _auth_client(user).post(_url(event.id), ["同事"], format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert fake_engine.calls == []
    assert RestaurantRecommendationRequest.objects.count() == 0


def test_situational_items_are_trimmed_and_blank_items_dropped(fake_engine):
    user = _create_user()
    event = _create_finalized_event(user)

    response = _auth_client(user).post(
        _url(event.id), {"situational": [" 可久坐 ", "  ", "有插座"]}, format="json"
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["resolvedPreferences"]["situational"] == ["可久坐", "有插座"]


def test_situational_duplicates_after_trim_are_rejected(fake_engine):
    user = _create_user()
    event = _create_finalized_event(user)

    response = _auth_client(user).post(
        _url(event.id), {"situational": ["可久坐", " 可久坐"]}, format="json"
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert [e["field"] for e in response.json()["errors"]] == ["situational"]


def test_failure_while_saving_success_marks_failed_not_pending(fake_engine, caplog):
    """引擎成功但寫入結果失敗(例如結果無法存成 JSON)→ 不可殘留 pending
    佔用額度,紀錄轉為 failed/UNEXPECTED_ERROR 並記 ai_rec.failed。"""
    caplog.set_level(logging.INFO)
    fake_engine.result = RecommendationResult(
        restaurants=[{"id": "r1", "name": "n", "address": "a", "bad": object()}],
        notes=None,
        usage=None,
        model="m",
    )
    user = _create_user()
    event = _create_finalized_event(user)
    client = _auth_client(user)
    client.raise_request_exception = False

    response = client.post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    record = RestaurantRecommendationRequest.objects.get()
    assert record.status == Status.FAILED
    assert record.error_code == "UNEXPECTED_ERROR"
    assert len(_events(caplog, "ai_rec.failed")) == 1
    assert _events(caplog, "ai_rec.succeeded") == []
    assert _used(client) == 0
