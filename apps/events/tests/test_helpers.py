from datetime import UTC, date, datetime

import pytest
from django.utils import timezone

from .helpers import taipei_today_plus


def test_taipei_today_plus_uses_taipei_date_not_utc_date(monkeypatch):
    """台灣 9/29 00:30(UTC 仍為 9/28 16:30)→ 今天是 9/29,不是 UTC 的 9/28。"""
    monkeypatch.setattr(timezone, "now", lambda: datetime(2026, 9, 28, 16, 30, tzinfo=UTC))

    assert taipei_today_plus(0) == date(2026, 9, 29)


def test_taipei_today_plus_offsets_across_month_and_year(monkeypatch):
    """跨月、跨年的正負位移。"""
    monkeypatch.setattr(timezone, "now", lambda: datetime(2026, 12, 30, 20, 0, tzinfo=UTC))

    # 台灣時間 2026-12-31 04:00
    assert taipei_today_plus(1) == date(2027, 1, 1)
    assert taipei_today_plus(-31) == date(2026, 11, 30)


def test_taipei_today_plus_rejects_non_int_days():
    with pytest.raises(TypeError):
        taipei_today_plus("2")
