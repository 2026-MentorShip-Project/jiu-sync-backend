from datetime import timedelta

EXPIRY_THRESHOLD = timedelta(days=7)


def compute_display_status(
    status, response_deadline, finalized_at, cancelled_at, final_slot_date, now
):
    """Derive an event's display status without touching the database.

    Pure function: takes plain values (no Django model access) so it can be
    unit tested directly and reused by future finalize/cancel/live-polling
    code without duplicating this logic.
    """
    if status == "active":
        if now < response_deadline:
            return "voting_open"
        return "voting_closed_pending"

    if status == "finalized":
        if now - finalized_at < EXPIRY_THRESHOLD:
            if final_slot_date >= now.date():
                return "finalized_upcoming"
            return "finalized_past"
        return "link_expired"

    if status == "cancelled":
        if now - cancelled_at < EXPIRY_THRESHOLD:
            return "cancelled"
        return "link_expired"
