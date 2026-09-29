"""每月 AI 推薦額度:計算規則(D3/D5/D6)與 `GET /api/me/ai-recommendation-quota/`。

見 openspec/changes/add-ai-restaurant-recommendation/tasks.md 1.1、
specs/restaurant-recommendations/spec.md「每人每月使用次數上限」「查詢當月額度」。

兩個 seam:
- HTTP(`APIClient`):回應形狀、認證、設定生效、使用者隔離。
- `compute_quota(user, now=...)`:需要固定時間的月份邊界、5 分鐘 pending 過期、
  重置時間(不引入 freezegun,直接傳 `now`)。
"""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User
from apps.events.models import Event
from apps.recommendations.models import RestaurantRecommendationRequest
from apps.recommendations.quota import compute_quota

pytestmark = pytest.mark.django_db

QUOTA_URL = "/api/me/ai-recommendation-quota/"
TAIPEI = ZoneInfo("Asia/Taipei")

Status = RestaurantRecommendationRequest.Status


def _taipei(*args):
    return datetime(*args, tzinfo=TAIPEI)


def _create_user(email="host@example.com", google_sub="sub-1"):
    return User.objects.create_user(
        email=email, google_sub=google_sub, display_name="Host", avatar_url=""
    )


def _auth_client(user):
    access_token = str(RefreshToken.for_user(user).access_token)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")
    return client


def _create_event(owner):
    return Event.objects.create(
        owner=owner,
        title="聚餐",
        host_nickname="小明",
        mode=Event.Mode.DATE_ONLY,
        response_deadline=timezone.now() + timedelta(days=3),
    )


def _create_record(user, *, status, created_at=None, quota_period=None, event=None):
    """直接建推薦紀錄當前置資料。`quota_period` 未給時依 `created_at` 的台灣時間推導,
    與 D6「歸屬建立時月份」一致。"""
    created_at = created_at or timezone.now()
    if quota_period is None:
        quota_period = created_at.astimezone(TAIPEI).strftime("%Y-%m")
    return RestaurantRecommendationRequest.objects.create(
        user=user,
        event=event or _create_event(user),
        quota_period=quota_period,
        status=status,
        preferences={},
        model="preset:low",
        created_at=created_at,
    )


def _current_period():
    return timezone.now().astimezone(TAIPEI).strftime("%Y-%m")


# ---------------------------------------------------------------------------
# HTTP seam
# ---------------------------------------------------------------------------


@override_settings(PERPLEXITY_API_KEY="test-key")
def test_quota_without_records_returns_full_quota_for_current_taipei_month():
    """① 無紀錄 → limit 20, used 0, remaining 20, available true;
    period 為台灣時間當月、resetsAt 為下月 1 日 00:00+08:00。"""
    user = _create_user()

    response = _auth_client(user).get(QUOTA_URL)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["limit"] == 20
    assert body["used"] == 0
    assert body["remaining"] == 20
    assert body["available"] is True
    assert body["serviceAvailable"] is True
    assert body["period"] == _current_period()
    assert re.fullmatch(r"\d{4}-\d{2}", body["period"])
    # 下月 1 日 00:00 台灣時間;年月由 period 獨立推算(12 月 → 隔年 1 月)。
    year, month = map(int, body["period"].split("-"))
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    assert body["resetsAt"] == f"{next_year:04d}-{next_month:02d}-01T00:00:00+08:00"
    assert set(body) == {
        "period",
        "limit",
        "used",
        "remaining",
        "available",
        "resetsAt",
        "serviceAvailable",
    }


def test_quota_counts_current_month_succeeded_records():
    """② 3 筆當月 succeeded → used 3, remaining 17。"""
    user = _create_user()
    for _ in range(3):
        _create_record(user, status=Status.SUCCEEDED)

    body = _auth_client(user).get(QUOTA_URL).json()

    assert body["used"] == 3
    assert body["remaining"] == 17
    assert body["available"] is True


def test_quota_does_not_count_failed_records():
    """③ failed 不計次。"""
    user = _create_user()
    _create_record(user, status=Status.SUCCEEDED)
    for _ in range(4):
        _create_record(user, status=Status.FAILED)

    body = _auth_client(user).get(QUOTA_URL).json()

    assert body["used"] == 1
    assert body["remaining"] == 19


def test_quota_counts_fresh_pending_but_not_stale_pending():
    """④ 建立 < 5 分鐘的 pending 計入,> 5 分鐘不計(HTTP 版,真實時間)。"""
    user = _create_user()
    now = timezone.now()
    # quota_period 明確帶當月:否則在每月第一分鐘執行時,1 分鐘前的 pending 會被歸到上月。
    _create_record(
        user,
        status=Status.PENDING,
        created_at=now - timedelta(minutes=1),
        quota_period=_current_period(),
    )
    _create_record(
        user,
        status=Status.PENDING,
        created_at=now - timedelta(minutes=10),
        quota_period=_current_period(),
    )

    body = _auth_client(user).get(QUOTA_URL).json()

    assert body["used"] == 1
    assert body["remaining"] == 19


def test_quota_exhausted_at_limit():
    """⑥ 20 筆 → remaining 0, available false。"""
    user = _create_user()
    event = _create_event(user)
    for _ in range(20):
        _create_record(user, status=Status.SUCCEEDED, event=event)

    body = _auth_client(user).get(QUOTA_URL).json()

    assert body["used"] == 20
    assert body["remaining"] == 0
    assert body["available"] is False


def test_quota_remaining_never_negative_when_used_exceeds_lowered_limit():
    """邊界:上限被調低到比已用次數還少(例如 env 由 20 改 5)→ remaining 0 不為負。"""
    user = _create_user()
    event = _create_event(user)
    for _ in range(7):
        _create_record(user, status=Status.SUCCEEDED, event=event)

    with override_settings(AI_RECOMMENDATION_QUOTA_PER_USER=5):
        body = _auth_client(user).get(QUOTA_URL).json()

    assert body["limit"] == 5
    assert body["used"] == 7
    assert body["remaining"] == 0
    assert body["available"] is False


def test_quota_ignores_other_users_records():
    """⑦ 別的使用者的紀錄不計。"""
    user = _create_user()
    other = _create_user(email="other@example.com", google_sub="sub-2")
    for _ in range(5):
        _create_record(other, status=Status.SUCCEEDED)
    _create_record(other, status=Status.PENDING)
    _create_record(user, status=Status.SUCCEEDED)

    body = _auth_client(user).get(QUOTA_URL).json()

    assert body["used"] == 1


@override_settings(AI_RECOMMENDATION_QUOTA_PER_USER=5)
def test_quota_limit_follows_setting():
    """⑧ AI_RECOMMENDATION_QUOTA_PER_USER=5 生效。"""
    user = _create_user()
    for _ in range(3):
        _create_record(user, status=Status.SUCCEEDED)

    body = _auth_client(user).get(QUOTA_URL).json()

    assert body["limit"] == 5
    assert body["used"] == 3
    assert body["remaining"] == 2
    assert body["available"] is True


@override_settings(PERPLEXITY_API_KEY="")
def test_quota_still_200_with_service_unavailable_when_api_key_missing():
    """⑨ PERPLEXITY_API_KEY="" → 仍 200,serviceAvailable false,額度照常計算。"""
    user = _create_user()
    _create_record(user, status=Status.SUCCEEDED)

    response = _auth_client(user).get(QUOTA_URL)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["serviceAvailable"] is False
    assert body["used"] == 1
    assert body["available"] is True


@override_settings(PERPLEXITY_API_KEY="   ")
def test_quota_treats_whitespace_only_api_key_as_unavailable():
    """邊界:只有空白的 key(例如 .env 寫成 `PERPLEXITY_API_KEY= `)視同未設定。"""
    user = _create_user()

    body = _auth_client(user).get(QUOTA_URL).json()

    assert body["serviceAvailable"] is False


def test_quota_requires_authentication():
    """⑩ 未登入 → 401。"""
    response = APIClient().get(QUOTA_URL)

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json()["code"] == "UNAUTHORIZED"


def test_quota_rejects_invalid_token():
    """⑩ 帶無效 access token → 401。"""
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-token")

    response = client.get(QUOTA_URL)

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# compute_quota seam(固定時間)
# ---------------------------------------------------------------------------


def test_compute_quota_period_and_resets_at_for_fixed_time():
    user = _create_user()

    quota = compute_quota(user, now=_taipei(2026, 9, 15, 12, 0))

    assert quota.period == "2026-09"
    assert quota.resets_at.isoformat() == "2026-10-01T00:00:00+08:00"
    assert quota.limit == 20
    assert quota.used == 0
    assert quota.remaining == 20
    assert quota.available is True


def test_compute_quota_resets_at_rolls_over_year_in_december():
    """邊界:12 月 → 下次重置為隔年 1 月 1 日。"""
    user = _create_user()

    quota = compute_quota(user, now=_taipei(2026, 12, 31, 23, 59))

    assert quota.period == "2026-12"
    assert quota.resets_at.isoformat() == "2027-01-01T00:00:00+08:00"


def test_compute_quota_uses_taipei_month_not_utc_month():
    """邊界:UTC 9/30 16:30 = 台灣 10/1 00:30 → 屬於 10 月。"""
    user = _create_user()

    quota = compute_quota(user, now=datetime(2026, 9, 30, 16, 30, tzinfo=ZoneInfo("UTC")))

    assert quota.period == "2026-10"
    assert quota.resets_at.isoformat() == "2026-11-01T00:00:00+08:00"


def test_compute_quota_previous_month_succeeded_not_counted_at_month_boundary():
    """⑤ 台灣時間 9/30 23:59 的 succeeded 不計入 10/1 00:00 的額度;
    10/1 00:00 建立的則計入。"""
    user = _create_user()
    _create_record(user, status=Status.SUCCEEDED, created_at=_taipei(2026, 9, 30, 23, 59))
    _create_record(user, status=Status.SUCCEEDED, created_at=_taipei(2026, 9, 30, 23, 59, 59))

    october = compute_quota(user, now=_taipei(2026, 10, 1, 0, 0))
    september = compute_quota(user, now=_taipei(2026, 9, 30, 23, 59, 59))

    assert october.used == 0
    assert october.remaining == 20
    assert september.used == 2

    _create_record(user, status=Status.SUCCEEDED, created_at=_taipei(2026, 10, 1, 0, 0))

    assert compute_quota(user, now=_taipei(2026, 10, 1, 0, 1)).used == 1


def test_compute_quota_counts_by_creation_month_even_if_completed_later():
    """D6:9/30 23:59 建立、跨午夜才完成的請求記在 9 月(依 quota_period),
    不因 completed_at 在 10 月而算進 10 月。"""
    user = _create_user()
    record = _create_record(
        user, status=Status.SUCCEEDED, created_at=_taipei(2026, 9, 30, 23, 59, 50)
    )
    record.completed_at = _taipei(2026, 10, 1, 0, 0, 20)
    record.save()

    assert compute_quota(user, now=_taipei(2026, 10, 1, 0, 1)).used == 0
    assert compute_quota(user, now=_taipei(2026, 9, 30, 23, 59, 59)).used == 1


def test_compute_quota_pending_expiry_boundary_is_five_minutes():
    """④ pending 建立 4:59 前 → 計入;剛好 5:00 與 5:01 前 → 不計(created_at > now - 5 分鐘)。"""
    user = _create_user()
    now = _taipei(2026, 9, 15, 12, 0)
    _create_record(user, status=Status.PENDING, created_at=now - timedelta(minutes=4, seconds=59))
    _create_record(user, status=Status.PENDING, created_at=now - timedelta(minutes=5))
    _create_record(user, status=Status.PENDING, created_at=now - timedelta(minutes=5, seconds=1))

    quota = compute_quota(user, now=now)

    assert quota.used == 1


def test_compute_quota_previous_month_fresh_pending_not_counted_in_new_month():
    """邊界:9/30 23:58 建立、尚未過期的 pending 歸屬 9 月,不佔用 10 月的額度。"""
    user = _create_user()
    _create_record(user, status=Status.PENDING, created_at=_taipei(2026, 9, 30, 23, 58))

    assert compute_quota(user, now=_taipei(2026, 10, 1, 0, 0)).used == 0
    assert compute_quota(user, now=_taipei(2026, 9, 30, 23, 59)).used == 1


def test_compute_quota_mixed_statuses():
    """succeeded + 有效 pending 計入;failed + 過期 pending 不計。"""
    user = _create_user()
    now = _taipei(2026, 9, 15, 12, 0)
    for _ in range(2):
        _create_record(user, status=Status.SUCCEEDED, created_at=now - timedelta(days=3))
    _create_record(user, status=Status.PENDING, created_at=now - timedelta(seconds=10))
    _create_record(user, status=Status.PENDING, created_at=now - timedelta(hours=1))
    _create_record(user, status=Status.FAILED, created_at=now - timedelta(minutes=1))

    quota = compute_quota(user, now=now)

    assert quota.used == 3
    assert quota.remaining == 17


@override_settings(AI_RECOMMENDATION_QUOTA_PER_USER=0)
def test_compute_quota_zero_limit_means_unavailable():
    """邊界:上限設 0(例如暫時停用)→ available false、remaining 0。"""
    user = _create_user()

    quota = compute_quota(user, now=_taipei(2026, 9, 15, 12, 0))

    assert quota.limit == 0
    assert quota.remaining == 0
    assert quota.available is False
