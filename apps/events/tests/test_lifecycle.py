from datetime import timedelta

from django.utils import timezone

from apps.events.lifecycle import compute_display_status

NOW = timezone.make_aware(timezone.datetime(2026, 9, 16, 12, 0, 0))


def test_active_before_deadline_is_voting_open():
    """狀態為進行中且目前時間早於投票截止時間 → voting_open。"""
    result = compute_display_status(
        status="active",
        response_deadline=NOW + timedelta(hours=1),
        finalized_at=None,
        cancelled_at=None,
        final_slot_date=None,
        now=NOW,
    )

    assert result == "voting_open"


def test_active_at_or_after_deadline_is_voting_closed_pending():
    """狀態為進行中且目前時間不早於投票截止時間(含剛好等於)→ voting_closed_pending。"""
    result = compute_display_status(
        status="active",
        response_deadline=NOW,
        finalized_at=None,
        cancelled_at=None,
        final_slot_date=None,
        now=NOW,
    )

    assert result == "voting_closed_pending"


def test_finalized_within_7_days_and_slot_date_in_future_is_finalized_upcoming():
    """狀態為已定案、距定案時間未滿 7 天、最終場次日期尚未到來 → finalized_upcoming。"""
    result = compute_display_status(
        status="finalized",
        response_deadline=NOW - timedelta(days=10),
        finalized_at=NOW - timedelta(days=1),
        cancelled_at=None,
        final_slot_date=(NOW + timedelta(days=2)).date(),
        now=NOW,
    )

    assert result == "finalized_upcoming"


def test_finalized_within_7_days_and_slot_date_passed_is_finalized_past():
    """狀態為已定案、距定案時間未滿 7 天、最終場次日期已過 → finalized_past。"""
    result = compute_display_status(
        status="finalized",
        response_deadline=NOW - timedelta(days=10),
        finalized_at=NOW - timedelta(days=1),
        cancelled_at=None,
        final_slot_date=(NOW - timedelta(days=1)).date(),
        now=NOW,
    )

    assert result == "finalized_past"


def test_finalized_7_days_or_more_ago_is_link_expired():
    """狀態為已定案且距定案時間已滿 7 天(含剛好等於 7 天)→ link_expired。"""
    result = compute_display_status(
        status="finalized",
        response_deadline=NOW - timedelta(days=20),
        finalized_at=NOW - timedelta(days=7),
        cancelled_at=None,
        final_slot_date=(NOW + timedelta(days=2)).date(),
        now=NOW,
    )

    assert result == "link_expired"


def test_cancelled_within_7_days_is_cancelled():
    """狀態為已取消且距取消時間未滿 7 天 → cancelled。"""
    result = compute_display_status(
        status="cancelled",
        response_deadline=NOW - timedelta(days=10),
        finalized_at=None,
        cancelled_at=NOW - timedelta(days=1),
        final_slot_date=None,
        now=NOW,
    )

    assert result == "cancelled"


def test_cancelled_7_days_or_more_ago_is_link_expired():
    """狀態為已取消且距取消時間已滿 7 天(含剛好等於 7 天)→ link_expired。"""
    result = compute_display_status(
        status="cancelled",
        response_deadline=NOW - timedelta(days=20),
        finalized_at=None,
        cancelled_at=NOW - timedelta(days=7),
        final_slot_date=None,
        now=NOW,
    )

    assert result == "link_expired"
