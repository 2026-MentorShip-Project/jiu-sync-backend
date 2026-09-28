"""每人每月 AI 推薦額度的計算(design.md D3/D5/D6)。

額度不存計數器,每次由 ``RestaurantRecommendationRequest`` 推導:
``used = succeeded(當月) + pending(當月, created_at > now - PENDING_EXPIRY)``。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import RestaurantRecommendationRequest

# pending 視為「進行中、佔用一次額度」的時限(D5)。必須大於任何活著的請求可能
# 存活的上限,否則 pending 過期後額度被多放出一次、原請求之後又成功計次而超用。
# 請求存活上限由引擎整體期限(PERPLEXITY_TIMEOUT_SECONDS,自送出請求起算)保證,
# 不依賴 gunicorn(gthread 的 --timeout 不限制單一請求);這組數字的關係由
# engines.check_settings() 在啟動時檢查 PENDING_EXPIRY > 整體期限 + 60 秒,
# 不成立即 ImproperlyConfigured——本註解只描述,不當作保證。
# process 被殺留下的 pending 5 分鐘後自動不計數。
PENDING_EXPIRY = timedelta(minutes=5)


@dataclass(frozen=True)
class QuotaStatus:
    period: str
    limit: int
    used: int
    remaining: int
    available: bool
    resets_at: datetime


def quota_period_for(now: datetime) -> str:
    """``YYYY-MM``,以 ``TIME_ZONE``(Asia/Taipei)的自然月計算(D6)。"""
    return timezone.localtime(now).strftime("%Y-%m")


def _next_month_start(now: datetime) -> datetime:
    local = timezone.localtime(now)
    year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
    return local.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)


def count_used(user, *, now: datetime) -> int:
    Status = RestaurantRecommendationRequest.Status
    return RestaurantRecommendationRequest.objects.filter(
        Q(status=Status.SUCCEEDED)
        | Q(status=Status.PENDING, created_at__gt=now - PENDING_EXPIRY),
        user=user,
        quota_period=quota_period_for(now),
    ).count()


def compute_quota(user, *, now: datetime | None = None) -> QuotaStatus:
    now = now or timezone.now()
    limit = settings.AI_RECOMMENDATION_QUOTA_PER_USER
    used = count_used(user, now=now)
    remaining = max(limit - used, 0)
    return QuotaStatus(
        period=quota_period_for(now),
        limit=limit,
        used=used,
        remaining=remaining,
        available=remaining > 0,
        resets_at=_next_month_start(now),
    )
