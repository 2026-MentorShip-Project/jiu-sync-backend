"""把請求條件與活動既有資訊合併成實際送出的條件(design.md D9)。

回傳值即回應的 ``resolvedPreferences``,也原樣存進 DB ``preferences`` 並交給引擎。
"""

from apps.events.models import ParticipantResponseSlotAvailability
from config.exceptions import ApiError


def _attending_count(event):
    """定案時段回覆 ``available`` 且未軟刪除的人數(與前端預填人數規格一致)。"""
    return ParticipantResponseSlotAvailability.objects.filter(
        slot_id=event.final_slot_id,
        availability=ParticipantResponseSlotAvailability.Availability.AVAILABLE,
        response__deleted_at__isnull=True,
    ).count()


def _meal_time(slot):
    if slot.time is not None:
        return slot.time.strftime("%H:%M")
    return (slot.label or "").strip() or None


def resolve_preferences(event, data):
    request_location = data.get("location") or None
    event_location = (event.location or "").strip() or None
    if request_location:
        location, location_source = request_location, "request"
    elif event_location:
        location, location_source = event_location, "event"
    else:
        raise ApiError(
            "請填寫聚餐地點，或先在活動設定地點", code="LOCATION_REQUIRED", status_code=400
        )

    party_size = data.get("partySize") or _attending_count(event)
    dietary = data.get("dietary") or {}
    slot = event.final_slot
    return {
        "location": location,
        "locationSource": location_source,
        "partySize": party_size,
        "mealDate": slot.date.isoformat(),
        "mealTime": _meal_time(slot),
        "relationship": data.get("relationship"),
        "budget": data.get("budget"),
        "situational": list(data.get("situational") or []),
        "dietary": {
            "vegetarian": dietary.get("vegetarian"),
            "spice": dietary.get("spice"),
            "cuisines": list(dietary.get("cuisines") or []),
            "restrictions": list(dietary.get("restrictions") or []),
        },
        "customPrompt": data.get("customPrompt") or None,
    }
