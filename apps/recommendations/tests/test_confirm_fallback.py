"""確認時 DB 兜底(tasks.md 5.4;design.md D4「確認時兜底」、D5、D6、D11)。

額度正確性不依賴「請求存活時間 < `PENDING_EXPIRY`」:轉 `succeeded` 前在 `User` 列鎖內,
若自己的 `pending` 已過期(`created_at <= now - PENDING_EXPIRY`),以紀錄自己的
`quota_period` 重算(不計自己);已達上限 → 紀錄轉 `failed`/`QUOTA_EXCEEDED_AT_CONFIRM`
(`result`/`usage` 仍保存),回 403 `AI_RECOMMENDATION_QUOTA_EXCEEDED`。

Seam:HTTP(DRF `APIClient`)。假引擎在 `recommend()` 內推進假時鐘、建立其他紀錄或刪除
活動,模擬「請求執行期間」的變化。假時鐘固定在月中(或刻意的月底),避免測試在真實
月份邊界執行時誤判(incident-log 2026-09-28)。
"""

import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone
from rest_framework import status

from apps.accounts.models import User
from apps.events.models import Event
from apps.recommendations.logging import JsonFormatter
from apps.recommendations.models import RestaurantRecommendationRequest
from apps.recommendations.quota import PENDING_EXPIRY
from apps.recommendations.tests.test_views import (
    _auth_client,
    _create_finalized_event,
    _create_user,
    _default_result,
    _events,
    _url,
)

pytestmark = pytest.mark.django_db

TAIPEI = ZoneInfo("Asia/Taipei")
Status = RestaurantRecommendationRequest.Status
LIMIT = 20
EXPIRED = PENDING_EXPIRY + timedelta(minutes=1)


class Clock:
    """取代 `django.utils.timezone.now` 的可推進假時鐘。"""

    def __init__(self, start):
        self.current = start

    def now(self):
        return self.current

    def advance(self, delta):
        self.current = self.current + delta


def _install_clock(monkeypatch, start):
    clock = Clock(start)
    monkeypatch.setattr(timezone, "now", clock.now)
    return clock


MID_MONTH = datetime(2026, 9, 15, 12, 0, tzinfo=TAIPEI)


class SideEffectEngine:
    """`recommend()` 內依序執行 `steps`(模擬請求執行期間的變化),再回傳預設結果。"""

    model_name = "preset:low"

    def __init__(self):
        self.result = _default_result()
        self.steps = []
        self.calls = 0

    def is_available(self):
        return True

    def recommend(self, context):
        self.calls += 1
        for step in self.steps:
            step()
        return self.result


@pytest.fixture
def engine(monkeypatch, settings):
    settings.AI_RECOMMENDATION_QUOTA_PER_USER = LIMIT
    engine = SideEffectEngine()
    monkeypatch.setattr("apps.recommendations.views.get_engine", lambda: engine)
    return engine


def _seed(user, event, count, *, period, created_at, record_status=Status.SUCCEEDED):
    RestaurantRecommendationRequest.objects.bulk_create(
        RestaurantRecommendationRequest(
            user=user,
            event=event,
            quota_period=period,
            status=record_status,
            preferences={},
            model="preset:low",
            created_at=created_at,
        )
        for _ in range(count)
    )


def _succeeded(user, period):
    return RestaurantRecommendationRequest.objects.filter(
        user=user, status=Status.SUCCEEDED, quota_period=period
    ).count()


def _json_line(record):
    return json.loads(JsonFormatter().format(record))


def _setup(monkeypatch, start=MID_MONTH):
    clock = _install_clock(monkeypatch, start)
    user = _create_user()
    event = _create_finalized_event(user)
    other_event = _create_finalized_event(user)
    return clock, user, event, other_event


# ---------------------------------------------------------------------------
# ① 已過期且同月其他 succeeded 已達上限 → 403
# ---------------------------------------------------------------------------


def test_expired_pending_with_quota_filled_meanwhile_returns_403_and_is_not_counted(
    engine, monkeypatch, caplog
):
    clock, user, event, other_event = _setup(monkeypatch)
    _seed(user, other_event, LIMIT - 1, period="2026-09", created_at=MID_MONTH)

    def fill_quota_while_running():
        clock.advance(EXPIRED)
        _seed(user, other_event, 1, period="2026-09", created_at=clock.now())

    engine.steps = [fill_quota_while_running]
    caplog.set_level(logging.INFO, logger="apps.recommendations")
    client = _auth_client(user)

    response = client.post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["code"] == "AI_RECOMMENDATION_QUOTA_EXCEEDED"
    assert _succeeded(user, "2026-09") == LIMIT

    record = RestaurantRecommendationRequest.objects.get(event=event)
    assert record.status == Status.FAILED
    assert record.error_code == "QUOTA_EXCEEDED_AT_CONFIRM"
    expected = _default_result()
    assert record.result == {"restaurants": expected.restaurants, "notes": expected.notes}
    assert record.usage == expected.usage
    assert record.completed_at is not None

    [failed] = _events(caplog, "ai_rec.failed")
    assert failed.levelno == logging.WARNING
    line = _json_line(failed)
    assert line["error_code"] == "QUOTA_EXCEEDED_AT_CONFIRM"
    assert line["cost_usd"] == 0.0123
    assert line["request_id"] == str(record.id)
    assert line["upstream_status"] is None
    assert "record_missing" not in line
    assert _events(caplog, "ai_rec.succeeded") == []

    # body 與一般「額度用完」(預留時就被擋下)完全相同。
    ordinary = client.post(_url(event.id), {}, format="json")
    assert ordinary.status_code == status.HTTP_403_FORBIDDEN
    assert response.json() == ordinary.json()
    assert engine.calls == 1


def test_expired_pending_over_limit_also_returns_403(engine, monkeypatch):
    """期間內其他紀錄已超過上限(極端)也同樣 403,不計次。"""
    clock, user, event, other_event = _setup(monkeypatch)
    _seed(user, other_event, LIMIT - 1, period="2026-09", created_at=MID_MONTH)

    def overfill():
        clock.advance(EXPIRED)
        _seed(user, other_event, 3, period="2026-09", created_at=clock.now())

    engine.steps = [overfill]

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert RestaurantRecommendationRequest.objects.get(event=event).status == Status.FAILED


def test_fresh_pending_of_other_request_counts_in_recheck(engine, monkeypatch):
    """重算沿用額度定義:其他未過期 pending 也佔用額度。"""
    clock, user, event, other_event = _setup(monkeypatch)
    _seed(user, other_event, LIMIT - 1, period="2026-09", created_at=MID_MONTH)

    def other_request_reserves():
        clock.advance(EXPIRED)
        _seed(
            user, other_event, 1, period="2026-09", created_at=clock.now(),
            record_status=Status.PENDING,
        )

    engine.steps = [other_request_reserves]

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_403_FORBIDDEN
    record = RestaurantRecommendationRequest.objects.get(event=event)
    assert record.error_code == "QUOTA_EXCEEDED_AT_CONFIRM"


# ---------------------------------------------------------------------------
# ② 已過期但未達上限 → 201 照常計次
# ---------------------------------------------------------------------------


def test_expired_pending_with_room_left_succeeds_and_counts(engine, monkeypatch, caplog):
    clock, user, event, other_event = _setup(monkeypatch)
    _seed(user, other_event, LIMIT - 2, period="2026-09", created_at=MID_MONTH)

    def one_more_meanwhile():
        clock.advance(EXPIRED)
        _seed(user, other_event, 1, period="2026-09", created_at=clock.now())

    engine.steps = [one_more_meanwhile]
    caplog.set_level(logging.INFO, logger="apps.recommendations")

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    record = RestaurantRecommendationRequest.objects.get(event=event)
    assert record.status == Status.SUCCEEDED
    assert record.error_code is None
    assert _succeeded(user, "2026-09") == LIMIT
    assert _events(caplog, "ai_rec.failed") == []
    assert len(_events(caplog, "ai_rec.succeeded")) == 1


def test_stale_pending_of_other_request_is_not_counted_in_recheck(engine, monkeypatch):
    """其他已過期的 pending 不佔用額度(與 compute_quota 定義一致)。"""
    clock, user, event, other_event = _setup(monkeypatch)
    _seed(user, other_event, LIMIT - 1, period="2026-09", created_at=MID_MONTH)
    _seed(
        user, other_event, 3, period="2026-09", created_at=MID_MONTH - EXPIRED,
        record_status=Status.PENDING,
    )
    _seed(user, other_event, 3, period="2026-09", created_at=MID_MONTH,
          record_status=Status.FAILED)
    engine.steps = [lambda: clock.advance(EXPIRED)]

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    assert _succeeded(user, "2026-09") == LIMIT


def test_other_users_records_are_not_counted_in_recheck(engine, monkeypatch):
    clock, user, event, other_event = _setup(monkeypatch)
    stranger = User.objects.create_user(
        email="x@example.com", google_sub="sub-x", display_name="X", avatar_url=""
    )
    stranger_event = _create_finalized_event(stranger)
    _seed(stranger, stranger_event, LIMIT, period="2026-09", created_at=MID_MONTH)
    engine.steps = [lambda: clock.advance(EXPIRED)]

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_201_CREATED


# ---------------------------------------------------------------------------
# ③ 未過期 → 201,不重算
# ---------------------------------------------------------------------------


def test_fresh_pending_succeeds_without_recheck_even_if_others_reached_limit(
    engine, monkeypatch
):
    clock, user, event, other_event = _setup(monkeypatch)
    _seed(user, other_event, LIMIT - 1, period="2026-09", created_at=MID_MONTH)

    def fill_quota_but_stay_fresh():
        clock.advance(PENDING_EXPIRY - timedelta(minutes=1))
        _seed(user, other_event, 1, period="2026-09", created_at=clock.now())

    engine.steps = [fill_quota_but_stay_fresh]

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    assert RestaurantRecommendationRequest.objects.get(event=event).status == Status.SUCCEEDED


def test_one_microsecond_before_expiry_is_not_expired(engine, monkeypatch):
    clock, user, event, other_event = _setup(monkeypatch)
    _seed(user, other_event, LIMIT - 1, period="2026-09", created_at=MID_MONTH)

    def fill_quota():
        clock.advance(PENDING_EXPIRY - timedelta(microseconds=1))
        _seed(user, other_event, 1, period="2026-09", created_at=clock.now())

    engine.steps = [fill_quota]

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_201_CREATED


# ---------------------------------------------------------------------------
# ④ 跨月:以紀錄自己的 quota_period 重算
# ---------------------------------------------------------------------------

MONTH_END = datetime(2026, 9, 30, 23, 58, tzinfo=TAIPEI)


def test_cross_month_recheck_uses_records_own_period_full_last_month_returns_403(
    engine, monkeypatch
):
    clock, user, event, other_event = _setup(monkeypatch, start=MONTH_END)
    _seed(user, other_event, LIMIT - 1, period="2026-09", created_at=MONTH_END)

    def fill_september_then_cross_midnight():
        _seed(user, other_event, 1, period="2026-09", created_at=clock.now())
        clock.advance(EXPIRED)  # → 2026-10-01 00:04 +08:00

    engine.steps = [fill_september_then_cross_midnight]

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["code"] == "AI_RECOMMENDATION_QUOTA_EXCEEDED"
    record = RestaurantRecommendationRequest.objects.get(event=event)
    assert record.quota_period == "2026-09"
    assert record.error_code == "QUOTA_EXCEEDED_AT_CONFIRM"
    assert _succeeded(user, "2026-09") == LIMIT


def test_cross_month_recheck_ignores_new_month_usage_when_last_month_has_room(
    engine, monkeypatch
):
    clock, user, event, other_event = _setup(monkeypatch, start=MONTH_END)
    _seed(user, other_event, LIMIT - 2, period="2026-09", created_at=MONTH_END)
    october = datetime(2026, 10, 1, 0, 1, tzinfo=TAIPEI)
    _seed(user, other_event, LIMIT, period="2026-10", created_at=october)
    engine.steps = [lambda: clock.advance(EXPIRED)]

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    record = RestaurantRecommendationRequest.objects.get(event=event)
    assert record.status == Status.SUCCEEDED
    assert record.quota_period == "2026-09"
    assert _succeeded(user, "2026-09") == LIMIT - 1
    assert _succeeded(user, "2026-10") == LIMIT


# ---------------------------------------------------------------------------
# ⑤ 紀錄在請求期間被 cascade 刪除
# ---------------------------------------------------------------------------


def test_expired_and_full_with_record_deleted_still_returns_403_and_flags_record_missing(
    engine, monkeypatch, caplog
):
    clock, user, event, other_event = _setup(monkeypatch)
    _seed(user, other_event, LIMIT - 1, period="2026-09", created_at=MID_MONTH)

    def fill_and_delete_event():
        clock.advance(EXPIRED)
        _seed(user, other_event, 1, period="2026-09", created_at=clock.now())
        Event.objects.filter(pk=event.pk).delete()

    engine.steps = [fill_and_delete_event]
    caplog.set_level(logging.INFO, logger="apps.recommendations")

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["code"] == "AI_RECOMMENDATION_QUOTA_EXCEEDED"
    assert not RestaurantRecommendationRequest.objects.filter(event_id=event.id).exists()
    assert _succeeded(user, "2026-09") == LIMIT

    [failed] = _events(caplog, "ai_rec.failed")
    assert failed.levelno == logging.WARNING
    line = _json_line(failed)
    assert line["error_code"] == "QUOTA_EXCEEDED_AT_CONFIRM"
    assert line["record_missing"] is True
    assert line["cost_usd"] == 0.0123
    assert _events(caplog, "ai_rec.succeeded") == []


def test_expired_with_room_and_record_deleted_keeps_record_missing_success(
    engine, monkeypatch, caplog
):
    clock, user, event, other_event = _setup(monkeypatch)
    _seed(user, other_event, LIMIT - 2, period="2026-09", created_at=MID_MONTH)

    def expire_and_delete_event():
        clock.advance(EXPIRED)
        Event.objects.filter(pk=event.pk).delete()

    engine.steps = [expire_and_delete_event]
    caplog.set_level(logging.INFO, logger="apps.recommendations")

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    assert [r["name"] for r in response.json()["restaurants"]] == ["好吃小館", "第二間"]
    [succeeded] = _events(caplog, "ai_rec.succeeded")
    assert succeeded.levelno == logging.INFO
    line = _json_line(succeeded)
    assert line["record_missing"] is True
    assert "error_code" not in line
    assert _events(caplog, "ai_rec.failed") == []


def test_user_deleted_during_request_keeps_record_missing_success(engine, monkeypatch, caplog):
    """使用者被刪除時 `User` 列已不存在:確認時拿鎖不可因此變成 500(D4 record_missing)。"""
    clock, user, event, _ = _setup(monkeypatch)

    def expire_and_delete_user():
        clock.advance(EXPIRED)
        User.objects.filter(pk=user.pk).delete()

    engine.steps = [expire_and_delete_user]
    caplog.set_level(logging.INFO, logger="apps.recommendations")

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    [succeeded] = _events(caplog, "ai_rec.succeeded")
    assert _json_line(succeeded)["record_missing"] is True


# ---------------------------------------------------------------------------
# ⑥ 邊界:created_at == now - PENDING_EXPIRY 視為過期
# ---------------------------------------------------------------------------


def test_exactly_at_expiry_is_treated_as_expired(engine, monkeypatch):
    clock, user, event, other_event = _setup(monkeypatch)
    _seed(user, other_event, LIMIT - 1, period="2026-09", created_at=MID_MONTH)

    def fill_at_exact_expiry():
        clock.advance(PENDING_EXPIRY)
        _seed(user, other_event, 1, period="2026-09", created_at=clock.now())

    engine.steps = [fill_at_exact_expiry]

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_403_FORBIDDEN
    record = RestaurantRecommendationRequest.objects.get(event=event)
    assert record.error_code == "QUOTA_EXCEEDED_AT_CONFIRM"
    assert _succeeded(user, "2026-09") == LIMIT
