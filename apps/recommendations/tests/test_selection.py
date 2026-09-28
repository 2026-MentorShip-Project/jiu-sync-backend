"""`PUT /api/events/{id}/selected-restaurant/` 主揪從推薦結果選定一間餐廳(tasks.md 6.1)。

Seam:HTTP(DRF `APIClient`)——選定 API 與活動詳情 `GET /api/events/{id}/` 的
`selectedRestaurant`。前置的「成功推薦紀錄」直接建立在 DB(推薦流程本身由
test_views.py 涵蓋)。

見 openspec/changes/add-ai-restaurant-recommendation/design.md D13 與
specs/restaurant-recommendations/spec.md「主揪可從推薦結果選定一間餐廳綁定到活動」。
"""

import ast
import json
import logging
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from django.db import OperationalError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.events.models import Event
from apps.events.tests.helpers import taipei_today_plus
from apps.recommendations import views as views_module
from apps.recommendations.logging import JsonFormatter
from apps.recommendations.models import (
    EventRestaurantSelection,
    RestaurantRecommendationRequest,
)
from apps.recommendations.quota import quota_period_for
from apps.recommendations.tests.test_views import (
    _auth_client,
    _create_finalized_event,
    _create_user,
    _default_result,
    _events,
)

pytestmark = pytest.mark.django_db

Status = RestaurantRecommendationRequest.Status
ErrorCode = RestaurantRecommendationRequest.ErrorCode

SELECTED_EVENT = "ai_rec.restaurant_selected"
HOLD_TIMEOUT = 1.0
JOIN_TIMEOUT = 20.0


def _url(event_id):
    return f"/api/events/{event_id}/selected-restaurant/"


def _detail_url(event_id):
    return f"/api/events/{event_id}/"


def _result_body(result=None):
    result = result or _default_result()
    return {"restaurants": result.restaurants, "notes": result.notes}


def _create_recommendation(
    event, *, user=None, record_status=Status.SUCCEEDED, result=..., error_code=None
):
    now = timezone.now()
    return RestaurantRecommendationRequest.objects.create(
        user=user or event.owner,
        event=event,
        quota_period=quota_period_for(now),
        status=record_status,
        preferences={},
        result=_result_body() if result is ... else result,
        error_code=error_code,
        model="preset:low",
        created_at=now,
        completed_at=None if record_status == Status.PENDING else now,
    )


def _restaurant(recommendation, ref):
    return next(r for r in recommendation.result["restaurants"] if r["id"] == ref)


def _body(recommendation, ref):
    return {"recommendationId": str(recommendation.id), "restaurantId": ref}


def _select_directly(event, recommendation, ref):
    """繞過 API 直接建立選擇(用於 API 本身不允許選定的狀態)。"""
    return EventRestaurantSelection.objects.create(
        event=event,
        recommendation=recommendation,
        restaurant_ref=ref,
        restaurant=_restaurant(recommendation, ref),
    )


def _selection_snapshot(event):
    selection = EventRestaurantSelection.objects.filter(event=event).first()
    if selection is None:
        return None
    return (
        selection.recommendation_id,
        selection.restaurant_ref,
        selection.restaurant,
        selection.selected_at,
        selection.updated_at,
    )


@pytest.fixture
def owner():
    return _create_user()


@pytest.fixture
def event(owner):
    return _create_finalized_event(owner)


@pytest.fixture
def recommendation(event):
    return _create_recommendation(event)


@pytest.fixture
def client(owner):
    return _auth_client(owner)


# ---------------------------------------------------------------------------
# ① 成功選定
# ---------------------------------------------------------------------------


def test_select_returns_200_with_snapshot_equal_to_recommended_restaurant(
    client, event, recommendation
):
    before = timezone.now()

    response = client.put(_url(event.id), _body(recommendation, "r2"), format="json")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert set(body) == {
        "recommendationId",
        "restaurantId",
        "restaurant",
        "selectedAt",
        "updatedAt",
    }
    assert body["recommendationId"] == str(recommendation.id)
    assert body["restaurantId"] == "r2"
    assert body["restaurant"] == _restaurant(recommendation, "r2")
    selected_at = datetime.fromisoformat(body["selectedAt"])
    assert selected_at >= before
    assert body["updatedAt"] == body["selectedAt"]

    selection = EventRestaurantSelection.objects.get(event=event)
    assert selection.recommendation_id == recommendation.id
    assert selection.restaurant_ref == "r2"
    assert selection.restaurant == _restaurant(recommendation, "r2")


def test_event_detail_shows_selected_restaurant(client, event, recommendation):
    put = client.put(_url(event.id), _body(recommendation, "r1"), format="json").json()

    detail = client.get(_detail_url(event.id)).json()

    assert detail["selectedRestaurant"] == {
        "restaurant": _restaurant(recommendation, "r1"),
        "selectedAt": put["selectedAt"],
    }


def test_snapshot_is_kept_even_if_recommendation_result_changes_later(
    client, event, recommendation
):
    """快照是選定當下複製的內容,不是即時解析推薦 JSON。"""
    original = _restaurant(recommendation, "r1")
    client.put(_url(event.id), _body(recommendation, "r1"), format="json")
    RestaurantRecommendationRequest.objects.filter(pk=recommendation.pk).update(
        result={"restaurants": [], "notes": None}
    )

    detail = client.get(_detail_url(event.id)).json()

    assert detail["selectedRestaurant"]["restaurant"] == original


# ---------------------------------------------------------------------------
# ② 重送同一間(冪等)
# ---------------------------------------------------------------------------


def test_resending_same_restaurant_returns_same_content_and_does_not_write(
    client, event, recommendation, caplog
):
    first = client.put(_url(event.id), _body(recommendation, "r1"), format="json").json()
    snapshot = _selection_snapshot(event)
    caplog.set_level(logging.INFO, logger="apps.recommendations")
    caplog.clear()  # 前置 PUT 的 log 也會被收(caplog handler 直接掛在該 logger),只看被測請求

    second = client.put(_url(event.id), _body(recommendation, "r1"), format="json")

    assert second.status_code == status.HTTP_200_OK
    assert second.json() == first
    assert _selection_snapshot(event) == snapshot
    [record] = _events(caplog, SELECTED_EVENT)
    assert record.is_change is False


# ---------------------------------------------------------------------------
# ③ 換一間(覆蓋)
# ---------------------------------------------------------------------------


def test_changing_to_another_restaurant_in_same_recommendation_overwrites(
    client, event, recommendation, caplog
):
    first = client.put(_url(event.id), _body(recommendation, "r1"), format="json").json()
    caplog.set_level(logging.INFO, logger="apps.recommendations")
    caplog.clear()  # 前置 PUT 的 log 也會被收(caplog handler 直接掛在該 logger),只看被測請求

    response = client.put(_url(event.id), _body(recommendation, "r2"), format="json")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["restaurantId"] == "r2"
    assert body["restaurant"] == _restaurant(recommendation, "r2")
    # selectedAt 為首次選定時間,updatedAt 為最後一次換選(D13)。
    assert body["selectedAt"] == first["selectedAt"]
    assert datetime.fromisoformat(body["updatedAt"]) > datetime.fromisoformat(first["updatedAt"])
    assert EventRestaurantSelection.objects.filter(event=event).count() == 1
    assert EventRestaurantSelection.objects.count() == 1
    detail = client.get(_detail_url(event.id)).json()
    assert detail["selectedRestaurant"]["restaurant"] == _restaurant(recommendation, "r2")
    [record] = _events(caplog, SELECTED_EVENT)
    assert record.is_change is True


def test_changing_to_restaurant_from_another_recommendation_overwrites(
    client, event, recommendation, caplog
):
    other_result = _default_result()
    other_result.restaurants[0] = {**other_result.restaurants[0], "name": "另一次的第一間"}
    another = _create_recommendation(event, result=_result_body(other_result))
    client.put(_url(event.id), _body(recommendation, "r1"), format="json")
    caplog.set_level(logging.INFO, logger="apps.recommendations")
    caplog.clear()  # 前置 PUT 的 log 也會被收(caplog handler 直接掛在該 logger),只看被測請求

    # 同樣是 r1,但來源推薦不同 → 視為換選。
    response = client.put(_url(event.id), _body(another, "r1"), format="json")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["recommendationId"] == str(another.id)
    assert response.json()["restaurant"]["name"] == "另一次的第一間"
    selection = EventRestaurantSelection.objects.get(event=event)
    assert selection.recommendation_id == another.id
    assert EventRestaurantSelection.objects.count() == 1
    [record] = _events(caplog, SELECTED_EVENT)
    assert record.is_change is True


# ---------------------------------------------------------------------------
# ④ 認證 / 擁有者 / 活動不存在
# ---------------------------------------------------------------------------


def test_unauthenticated_returns_401_and_selection_unchanged(client, event, recommendation):
    client.put(_url(event.id), _body(recommendation, "r1"), format="json")
    snapshot = _selection_snapshot(event)

    response = APIClient().put(_url(event.id), _body(recommendation, "r2"), format="json")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert _selection_snapshot(event) == snapshot


def test_non_owner_returns_403_and_selection_unchanged(client, event, recommendation):
    client.put(_url(event.id), _body(recommendation, "r1"), format="json")
    snapshot = _selection_snapshot(event)
    stranger = _create_user(email="other@example.com", google_sub="sub-2")

    response = _auth_client(stranger).put(
        _url(event.id), _body(recommendation, "r2"), format="json"
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert _selection_snapshot(event) == snapshot


def test_missing_event_returns_404_event_not_found(client, recommendation):
    response = client.put(_url("ZZZZZZZZ"), _body(recommendation, "r1"), format="json")

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["code"] == "EVENT_NOT_FOUND"
    assert EventRestaurantSelection.objects.count() == 0


def test_non_owner_with_invalid_body_still_gets_403(event):
    """檢查順序:擁有者(403)在 body 驗證(400)之前。"""
    stranger = _create_user(email="other@example.com", google_sub="sub-2")

    response = _auth_client(stranger).put(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# ⑤ 活動狀態
# ---------------------------------------------------------------------------


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
def test_not_finalized_event_returns_409_and_selection_unchanged(
    client, event, recommendation, mutate
):
    _select_directly(event, recommendation, "r1")
    snapshot = _selection_snapshot(event)
    mutate(event)
    event.save()

    response = client.put(_url(event.id), _body(recommendation, "r2"), format="json")

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "EVENT_NOT_FINALIZED"
    assert _selection_snapshot(event) == snapshot


def test_past_event_returns_409_event_already_past(owner, client):
    event = _create_finalized_event(owner, slot_date=taipei_today_plus(-1))
    recommendation = _create_recommendation(event)
    _select_directly(event, recommendation, "r1")
    snapshot = _selection_snapshot(event)

    response = client.put(_url(event.id), _body(recommendation, "r2"), format="json")

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "EVENT_ALREADY_PAST"
    assert _selection_snapshot(event) == snapshot


def test_event_today_can_still_be_selected(owner, client):
    event = _create_finalized_event(owner, slot_date=taipei_today_plus(0))
    recommendation = _create_recommendation(event)

    response = client.put(_url(event.id), _body(recommendation, "r1"), format="json")

    assert response.status_code == status.HTTP_200_OK


def test_link_expired_returns_410(owner, client):
    event = _create_finalized_event(owner, finalized_at=timezone.now() - timedelta(days=8))
    recommendation = _create_recommendation(event)
    _select_directly(event, recommendation, "r1")
    snapshot = _selection_snapshot(event)

    response = client.put(_url(event.id), _body(recommendation, "r2"), format="json")

    assert response.status_code == 410
    assert response.json()["code"] == "LINK_EXPIRED"
    assert _selection_snapshot(event) == snapshot


# ---------------------------------------------------------------------------
# ⑥ INVALID_RECOMMENDATION
# ---------------------------------------------------------------------------


def _assert_invalid_recommendation(response, event):
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "INVALID_RECOMMENDATION"
    assert EventRestaurantSelection.objects.filter(event=event).count() == 0


def test_nonexistent_recommendation_returns_invalid_recommendation(client, event):
    response = client.put(
        _url(event.id),
        {"recommendationId": str(uuid.uuid4()), "restaurantId": "r1"},
        format="json",
    )

    _assert_invalid_recommendation(response, event)


def test_recommendation_of_owners_other_event_returns_invalid_recommendation(owner, client, event):
    other_event = _create_finalized_event(owner)
    other = _create_recommendation(other_event)

    response = client.put(_url(event.id), _body(other, "r1"), format="json")

    _assert_invalid_recommendation(response, event)


def test_recommendation_of_someone_elses_event_returns_same_invalid_recommendation(client, event):
    """不透露其他活動的紀錄是否存在:與不存在時的回應完全相同。"""
    stranger = _create_user(email="other@example.com", google_sub="sub-2")
    other = _create_recommendation(_create_finalized_event(stranger))

    response = client.put(_url(event.id), _body(other, "r1"), format="json")
    missing = client.put(
        _url(event.id),
        {"recommendationId": str(uuid.uuid4()), "restaurantId": "r1"},
        format="json",
    )

    _assert_invalid_recommendation(response, event)
    assert response.json() == missing.json()


@pytest.mark.parametrize(
    ("record_status", "error_code", "has_result"),
    [
        (Status.FAILED, ErrorCode.QUOTA_EXCEEDED_AT_CONFIRM, True),
        (Status.FAILED, ErrorCode.UPSTREAM_TIMEOUT, False),
        (Status.PENDING, None, False),
        (Status.PENDING, None, True),
    ],
)
def test_not_succeeded_recommendation_returns_invalid_recommendation(
    client, event, record_status, error_code, has_result
):
    recommendation = _create_recommendation(
        event,
        record_status=record_status,
        error_code=error_code,
        result=_result_body() if has_result else None,
    )

    response = client.put(_url(event.id), _body(recommendation, "r1"), format="json")

    _assert_invalid_recommendation(response, event)


# ---------------------------------------------------------------------------
# ⑦ INVALID_RESTAURANT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("restaurant_id", ["r9", "", "R1", "name"])
def test_restaurant_not_in_result_returns_invalid_restaurant(
    client, event, recommendation, restaurant_id
):
    _select_directly(event, recommendation, "r1")
    snapshot = _selection_snapshot(event)

    response = client.put(_url(event.id), _body(recommendation, restaurant_id), format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "INVALID_RESTAURANT"
    assert _selection_snapshot(event) == snapshot


# ---------------------------------------------------------------------------
# ⑧ body 驗證
# ---------------------------------------------------------------------------


def _valid_body(recommendation):
    return _body(recommendation, "r1")


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"recommendationId": ...}, "recommendationId"),
        ({"restaurantId": ...}, "restaurantId"),
        ({"recommendationId": None}, "recommendationId"),
        ({"restaurantId": None}, "restaurantId"),
        ({"recommendationId": 123}, "recommendationId"),
        ({"recommendationId": ["x"]}, "recommendationId"),
        ({"restaurantId": 1}, "restaurantId"),
        ({"restaurantId": ["r1"]}, "restaurantId"),
        ({"restaurantId": {"id": "r1"}}, "restaurantId"),
        ({"recommendationId": "not-a-uuid"}, "recommendationId"),
        ({"recommendationId": ""}, "recommendationId"),
    ],
)
def test_invalid_body_returns_400_listing_field(client, event, recommendation, overrides, field):
    body = _valid_body(recommendation)
    for key, value in overrides.items():
        if value is ...:
            body.pop(key)
        else:
            body[key] = value

    response = client.put(_url(event.id), body, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert field in {e["field"] for e in response.json()["errors"]}
    assert EventRestaurantSelection.objects.count() == 0


def test_empty_body_lists_both_fields(client, event):
    response = client.put(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert {e["field"] for e in response.json()["errors"]} == {"recommendationId", "restaurantId"}


def test_non_object_body_returns_400(client, event):
    response = client.put(_url(event.id), ["x"], format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_body_validation_comes_before_event_status_check(client, event):
    """檢查順序:body 驗證(400)在狀態檢查(409)之前。"""
    _make_cancelled(event)
    event.save()

    response = client.put(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# ⑨ 活動詳情 / 列表
# ---------------------------------------------------------------------------


def test_event_detail_selected_restaurant_is_null_when_not_selected(client, event):
    detail = client.get(_detail_url(event.id)).json()

    assert "selectedRestaurant" in detail
    assert detail["selectedRestaurant"] is None


def test_participants_and_anonymous_can_see_selected_restaurant(client, event, recommendation):
    client.put(_url(event.id), _body(recommendation, "r1"), format="json")
    stranger = _create_user(email="other@example.com", google_sub="sub-2")

    for viewer in (APIClient(), _auth_client(stranger)):
        detail = viewer.get(_detail_url(event.id))
        assert detail.status_code == status.HTTP_200_OK
        assert detail.json()["selectedRestaurant"]["restaurant"] == _restaurant(
            recommendation, "r1"
        )


def _detail_queries(event):
    with CaptureQueriesContext(connection) as ctx:
        response = APIClient().get(_detail_url(event.id))
    assert response.status_code == status.HTTP_200_OK
    return [q["sql"] for q in ctx.captured_queries]


def test_selected_restaurant_does_not_add_queries_to_event_detail(
    event, recommendation, django_assert_num_queries
):
    baseline = len(_detail_queries(event))
    _select_directly(event, recommendation, "r1")

    with django_assert_num_queries(baseline):
        response = APIClient().get(_detail_url(event.id))

    assert response.json()["selectedRestaurant"] is not None
    # 選擇是和活動同一個查詢 JOIN 進來,不是另外查(select_related)。
    for sql in _detail_queries(event):
        assert 'FROM "recommendations_eventrestaurantselection"' not in sql


def test_event_detail_without_selection_does_not_query_selection_table(event):
    for sql in _detail_queries(event):
        assert 'FROM "recommendations_eventrestaurantselection"' not in sql


def test_event_list_does_not_include_selected_restaurant(owner, client, event, recommendation):
    client.put(_url(event.id), _body(recommendation, "r1"), format="json")

    response = client.get("/api/events/", {"owner": "me"})

    assert response.status_code == status.HTTP_200_OK
    [item] = response.json()
    assert "selectedRestaurant" not in item


def test_events_serializer_does_not_import_recommendations():
    """耦合只剩 related_name 字串(D13):events 不可反向依賴 recommendations。"""
    source = Path(__file__).resolve().parents[2] / "events" / "serializers.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any("recommendations" in name for name in imported)


# ---------------------------------------------------------------------------
# ⑩ 生命週期
# ---------------------------------------------------------------------------


def test_selection_survives_reopen_and_put_after_reopen_returns_409(client, event, recommendation):
    client.put(_url(event.id), _body(recommendation, "r1"), format="json")

    reopen = client.post(
        f"/api/events/{event.id}/reopen/",
        {"responseDeadline": (timezone.now() + timedelta(days=5)).isoformat()},
        format="json",
    )
    assert reopen.status_code == status.HTTP_200_OK

    detail = client.get(_detail_url(event.id)).json()
    assert detail["selectedRestaurant"]["restaurant"] == _restaurant(recommendation, "r1")

    response = client.put(_url(event.id), _body(recommendation, "r2"), format="json")
    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "EVENT_NOT_FINALIZED"
    assert EventRestaurantSelection.objects.get(event=event).restaurant_ref == "r1"


def test_selection_survives_cancel(client, event, recommendation):
    client.put(_url(event.id), _body(recommendation, "r1"), format="json")

    cancel = client.post(f"/api/events/{event.id}/cancel/", format="json")
    assert cancel.status_code == status.HTTP_200_OK

    detail = client.get(_detail_url(event.id)).json()
    assert detail["status"] == "cancelled"
    assert detail["selectedRestaurant"]["restaurant"] == _restaurant(recommendation, "r1")


# ---------------------------------------------------------------------------
# ⑪ 不改活動地點
# ---------------------------------------------------------------------------


def test_selecting_does_not_change_event_location(client, event, recommendation):
    original = event.location

    client.put(_url(event.id), _body(recommendation, "r1"), format="json")

    event.refresh_from_db()
    assert event.location == original
    assert client.get(_detail_url(event.id)).json()["location"] == original


# ---------------------------------------------------------------------------
# ⑫ log
# ---------------------------------------------------------------------------


def test_success_logs_restaurant_selected_with_whitelisted_fields(
    owner, client, event, recommendation, caplog
):
    caplog.set_level(logging.INFO, logger="apps.recommendations")

    client.put(_url(event.id), _body(recommendation, "r2"), format="json")

    [record] = _events(caplog, SELECTED_EVENT)
    assert record.levelno == logging.INFO
    line = json.loads(JsonFormatter().format(record))
    assert line["event"] == SELECTED_EVENT
    assert line["user_id"] == str(owner.id)
    assert line["event_id"] == event.id
    assert line["request_id"] == str(recommendation.id)
    assert line["restaurant_ref"] == "r2"
    assert line["is_change"] is False
    raw = JsonFormatter().format(record)
    restaurant = _restaurant(recommendation, "r2")
    assert restaurant["name"] not in raw
    assert restaurant["address"] not in raw


def test_change_log_is_change_true_in_json(client, event, recommendation, caplog):
    client.put(_url(event.id), _body(recommendation, "r1"), format="json")
    caplog.set_level(logging.INFO, logger="apps.recommendations")
    caplog.clear()  # 前置 PUT 的 log 也會被收(caplog handler 直接掛在該 logger),只看被測請求

    client.put(_url(event.id), _body(recommendation, "r2"), format="json")

    [record] = _events(caplog, SELECTED_EVENT)
    assert json.loads(JsonFormatter().format(record))["is_change"] is True


def test_logs_never_contain_restaurant_name_or_address(client, event, recommendation, caplog):
    caplog.set_level(logging.DEBUG)

    client.put(_url(event.id), _body(recommendation, "r1"), format="json")
    client.put(_url(event.id), _body(recommendation, "r2"), format="json")

    texts = [JsonFormatter().format(r) for r in caplog.records] + [
        r.getMessage() for r in caplog.records
    ]
    for ref in ("r1", "r2"):
        restaurant = _restaurant(recommendation, ref)
        for text in texts:
            assert restaurant["name"] not in text
            assert restaurant["address"] not in text


def test_error_responses_do_not_log_restaurant_selected(
    owner, client, event, recommendation, caplog
):
    stranger = _create_user(email="other@example.com", google_sub="sub-2")
    past_event = _create_finalized_event(owner, slot_date=taipei_today_plus(-1))
    past_recommendation = _create_recommendation(past_event)
    caplog.set_level(logging.INFO, logger="apps.recommendations")

    responses = [
        client.put(_url(event.id), {}, format="json"),  # 400
        client.put(_url(event.id), _body(recommendation, "r9"), format="json"),  # 400
        _auth_client(stranger).put(
            _url(event.id), _body(recommendation, "r1"), format="json"
        ),  # 403
        client.put(_url(past_event.id), _body(past_recommendation, "r1"), format="json"),  # 409
    ]

    assert [r.status_code for r in responses] == [400, 400, 403, 409]
    assert _events(caplog, SELECTED_EVENT) == []


# ---------------------------------------------------------------------------
# ⑬ 併發
# ---------------------------------------------------------------------------


class StatusCheckHold:
    """包住 view 用的 `compute_display_status`:算完後在 barrier 等另一個請求。

    有 `Event` 列鎖時,第二個請求卡在 `select_for_update`,到不了狀態檢查,barrier
    逾時後第一個請求才繼續寫入(`passed == 0`);沒有鎖時兩者會同時停在狀態檢查之後
    (`passed == 2`)——狀態檢查與寫入之間就有 reopen/cancel 的競態(D13)。
    只看結果抓不到這點:`update_or_create` 自己會處理 OneToOne 的 IntegrityError。
    """

    def __init__(self, parties, timeout):
        self.barrier = threading.Barrier(parties, timeout=timeout)
        self.calls = 0
        self.passed = 0
        self._lock = threading.Lock()

    def __call__(self, *args, **kwargs):
        result = self.original(*args, **kwargs)
        with self._lock:
            self.calls += 1
        try:
            self.barrier.wait()
        except threading.BrokenBarrierError:
            pass
        else:
            with self._lock:
                self.passed += 1
        return result


@pytest.mark.django_db(transaction=True)
def test_concurrent_puts_selecting_different_restaurants_both_succeed(monkeypatch):
    owner = _create_user()
    event = _create_finalized_event(owner)
    recommendation = _create_recommendation(event)
    hold = StatusCheckHold(2, HOLD_TIMEOUT)
    hold.original = views_module.compute_display_status
    monkeypatch.setattr(views_module, "compute_display_status", hold)

    results = {}
    errors = []
    start = threading.Barrier(2, timeout=JOIN_TIMEOUT)
    clients = {ref: _auth_client(owner) for ref in ("r1", "r2")}

    def send(ref):
        try:
            start.wait()
            response = clients[ref].put(_url(event.id), _body(recommendation, ref), format="json")
            results[ref] = (response.status_code, response.json())
        except Exception as exc:  # noqa: BLE001 — 轉交主執行緒斷言,不吞掉
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=send, args=(ref,)) for ref in ("r1", "r2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=JOIN_TIMEOUT)

    assert not any(t.is_alive() for t in threads), "request thread hung"
    assert errors == []
    assert hold.calls == 2
    # 兩個請求從未同時位於鎖內的狀態檢查之後(由 Event 列鎖序列化)。
    assert hold.passed == 0
    for ref in ("r1", "r2"):
        code, body = results[ref]
        assert code == status.HTTP_200_OK
        assert body["restaurantId"] == ref
        assert body["restaurant"] == _restaurant(recommendation, ref)
    assert EventRestaurantSelection.objects.filter(event=event).count() == 1
    assert EventRestaurantSelection.objects.get(event=event).restaurant_ref in {"r1", "r2"}


class HoldInsideLock:
    """包住 view 用的 `compute_display_status`(在 `Event` 列鎖內呼叫):
    通知主執行緒「已持鎖」,等主執行緒放行後才繼續寫入並 commit。"""

    def __init__(self, original):
        self.original = original
        self.locked = threading.Event()
        self.release = threading.Event()

    def __call__(self, *args, **kwargs):
        # 守住「同步點位於鎖內 transaction」:狀態檢查若被移到取鎖之前,
        # 這裡會先失敗,不會讓下方測試在沒持鎖的情況下空過。
        assert connection.in_atomic_block, "status check must run inside the locked transaction"
        result = self.original(*args, **kwargs)
        self.locked.set()
        self.release.wait(timeout=JOIN_TIMEOUT)
        return result


def _run_while_put_holds_event_lock(monkeypatch, other_connection_work):
    """PUT 在持有 `Event` 列鎖期間暫停,主執行緒的連線以 `lock_timeout = 1s`
    執行 `other_connection_work(event)`;回傳該工作丟出的例外(沒有則 None)。"""
    owner = _create_user()
    event = _create_finalized_event(owner)
    recommendation = _create_recommendation(event)
    hold = HoldInsideLock(views_module.compute_display_status)
    monkeypatch.setattr(views_module, "compute_display_status", hold)

    results = {}
    errors = []
    put_client = _auth_client(owner)

    def send():
        try:
            response = put_client.put(_url(event.id), _body(recommendation, "r1"), format="json")
            results["put"] = response.status_code
        except Exception as exc:  # noqa: BLE001 — 轉交主執行緒斷言,不吞掉
            errors.append(exc)
        finally:
            hold.locked.set()  # PUT 提早失敗時不讓主執行緒空等
            connection.close()

    thread = threading.Thread(target=send)
    thread.start()
    outcome = None
    try:
        assert hold.locked.wait(timeout=JOIN_TIMEOUT), "PUT never reached the lock"
        assert errors == []
        # commit 必須留在 try 內、且在放行 PUT 之前:Django 的 FK 是
        # DEFERRABLE INITIALLY DEFERRED,INSERT 對活動列取 KEY SHARE 的檢查
        # 發生在 commit 時,移到放行之後測試就會空過。
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '1s'")
                other_connection_work(event)
        except OperationalError as exc:
            outcome = exc
    finally:
        hold.release.set()
        thread.join(timeout=JOIN_TIMEOUT)

    assert not thread.is_alive(), "request thread hung"
    assert errors == []
    assert results["put"] == status.HTTP_200_OK
    return outcome


def _is_lock_timeout(exc):
    return exc is not None and getattr(exc.__cause__, "sqlstate", None) == "55P03"


@pytest.mark.django_db(transaction=True)
def test_put_holding_event_lock_does_not_block_new_recommendation_insert(monkeypatch):
    """D13:`FOR NO KEY UPDATE` 不與 INSERT 推薦紀錄時 FK 取的 `KEY SHARE` 衝突。"""

    def insert_recommendation(event):
        _create_recommendation(event, record_status=Status.PENDING, result=None)

    outcome = _run_while_put_holds_event_lock(monkeypatch, insert_recommendation)

    assert outcome is None, f"insert blocked by PUT's Event lock: {outcome!r}"
    assert RestaurantRecommendationRequest.objects.count() == 2


def _reopen_style_update(event):
    Event.objects.filter(pk=event.pk, status=Event.Status.FINALIZED).update(
        status=Event.Status.ACTIVE, final_slot=None, updated_at=timezone.now()
    )


def _cancel_style_update(event):
    now = timezone.now()
    Event.objects.filter(
        pk=event.pk, status__in=[Event.Status.ACTIVE, Event.Status.FINALIZED]
    ).update(status=Event.Status.CANCELLED, cancelled_at=now, final_slot=None, updated_at=now)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("update", [_reopen_style_update, _cancel_style_update])
def test_put_holding_event_lock_still_blocks_lifecycle_update(monkeypatch, update):
    """D13:reopen/cancel 對 `Event` 列的 UPDATE 仍須等 PUT 釋放鎖。

    兩者照 `apps/events/views.py` 的 `EventReopenView`/`EventCancelView` 條件式
    `.update()` 形狀(只改非 key 欄位);那邊的寫法改變時這裡要跟著看。
    """
    outcome = _run_while_put_holds_event_lock(monkeypatch, update)

    assert _is_lock_timeout(outcome), f"expected lock_timeout, got {outcome!r}"
    event = Event.objects.get()
    assert event.status == Event.Status.FINALIZED


# ---------------------------------------------------------------------------
# ⑭ cascade
# ---------------------------------------------------------------------------


def test_deleting_event_cascades_selection(client, event, recommendation):
    client.put(_url(event.id), _body(recommendation, "r1"), format="json")
    assert EventRestaurantSelection.objects.count() == 1

    event.delete()

    assert EventRestaurantSelection.objects.count() == 0
