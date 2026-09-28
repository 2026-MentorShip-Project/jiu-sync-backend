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

# pending 視為「進行中、佔用一次額度」的時限(D5)。額度正確性不依賴「請求存活
# 時間 < PENDING_EXPIRY」:轉 succeeded 前在 User 列鎖內做確認時兜底(D4,
# exceeds_quota_at_confirm),pending 已過期就以紀錄自己的月份重算,已滿額即不計次。
# engines.check_settings() 在啟動時檢查 PENDING_EXPIRY > PERPLEXITY_TIMEOUT_SECONDS
# + 60 秒,只是讓「請求活得比 pending 久」(浪費一次上游費用)變得罕見,不是正確性保證。
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


def _used_records(user_id, *, period: str, now: datetime):
    """計入額度的紀錄:``period`` 月的 succeeded + 未過期 pending(D3/D5)。"""
    Status = RestaurantRecommendationRequest.Status
    return RestaurantRecommendationRequest.objects.filter(
        Q(status=Status.SUCCEEDED)
        | Q(status=Status.PENDING, created_at__gt=now - PENDING_EXPIRY),
        user_id=user_id,
        quota_period=period,
    )


def count_used(user, *, now: datetime) -> int:
    return _used_records(user.pk, period=quota_period_for(now), now=now).count()


def exceeds_quota_at_confirm(record, *, now: datetime) -> bool:
    """確認時兜底(design.md D4):``record`` 的 pending 已過期時,它不再被計入額度,
    期間其他請求可能已把額度用滿。只在已過期(``created_at <= now - PENDING_EXPIRY``,
    與計數條件相反)時重算,以紀錄自己的 ``quota_period``(D6)計、不計入自己。

    呼叫端必須持有該使用者的 ``User`` 列鎖,且 ``now`` 在鎖內取得。
    """
    if record.created_at > now - PENDING_EXPIRY:
        return False
    used = (
        _used_records(record.user_id, period=record.quota_period, now=now)
        .exclude(pk=record.pk)
        .count()
    )
    return used >= settings.AI_RECOMMENDATION_QUOTA_PER_USER


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
