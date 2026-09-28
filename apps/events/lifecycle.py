from datetime import timedelta

from django.utils import timezone

EXPIRY_THRESHOLD = timedelta(days=7)


def compute_display_status(
    status, response_deadline, finalized_at, cancelled_at, final_slot_date, now
):
    """Derive an event's display status without touching the database.

    Pure function: takes plain values (no Django model access) so it can be
    unit tested directly and reused by future finalize/cancel/live-polling
    code without duplicating this logic.

    「聚會日期是否已過」以台灣時間(``settings.TIME_ZONE``,經 Django 目前啟用的
    時區)的今天判斷;``now`` 必須是 aware datetime,naive 會拋 ``ValueError``。
    """
    if status == "active":
        if now < response_deadline:
            return "voting_open"
        return "voting_closed_pending"

    if status == "finalized":
        if now - finalized_at < EXPIRY_THRESHOLD:
            # 以台灣時間(settings.TIME_ZONE)的今天判斷,不可用 now.date()(UTC 日期);
            # naive now 會由 localdate 拋 ValueError(design.md D1/D2)。
            if final_slot_date >= timezone.localdate(now):
                return "finalized_upcoming"
            return "finalized_past"
        return "link_expired"

    if status == "cancelled":
        if now - cancelled_at < EXPIRY_THRESHOLD:
            return "cancelled"
        return "link_expired"
