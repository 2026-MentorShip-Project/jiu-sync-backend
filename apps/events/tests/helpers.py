"""測試共用 helper(檔名不符合 ``test_*.py``,不會被 pytest 當成測試收集)。"""

from datetime import timedelta

from django.utils import timezone


def taipei_today_plus(days):
    """台灣時間(``settings.TIME_ZONE``)的今天 + ``days`` 天,回傳 ``date``。

    結果會隨「今天」改變的測試(例如依 ``displayStatus`` 判斷聚會日期是否已過)
    不得寫死日期字串,改用本 helper 產生(fix-display-status-timezone design.md D4)。
    刻意用 ``timezone.localdate()`` 而非 ``timezone.now().date()``:後者是 UTC 日期,
    台灣時間 00:00–08:00 會差一天。
    """
    return timezone.localdate() + timedelta(days=days)
