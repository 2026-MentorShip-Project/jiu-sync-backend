import uuid

from django.conf import settings
from django.db import models

from .ids import generate_short_id


class Slot(models.Model):
    """A candidate date/time option for an :class:`Event`.

    Kept as its own model (not a JSONField on ``Event``) so a future
    ``ParticipantResponse`` through-model can FK to a specific slot. See
    openspec/changes/add-events-api/design.md D2.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey("Event", on_delete=models.CASCADE, related_name="slots")
    date = models.DateField()
    time = models.TimeField(null=True, blank=True)
    label = models.CharField(max_length=100, null=True, blank=True)

    def __str__(self):
        return f"{self.date} {self.time or ''}".strip()


class Event(models.Model):
    """A gathering (揪團) created by a host (主揪) for participants to vote on.

    8-char base62 short id primary key (``ids.generate_short_id``) so event
    identifiers are safe to expose in shareable URLs without leaking a
    sequential, enumerable count of events — same non-enumerability
    rationale as the UUID used by ``apps.accounts.User`` and by ``Slot``
    below, just shorter so it reads cleanly in a shared URL. See
    openspec/changes/add-events-api/design.md D1 (amended 2026-09-16):
    originally a UUID like ``Slot``, changed to this short id per explicit
    owner decision — ``Slot`` stays UUID since its id is never surfaced in a
    URL.

    ``finalized_at``/``cancelled_at``/``final_slot``/``final_note`` have no
    write path yet in this change — they're a deliberate schema-ahead
    decision so a future finalize/cancel change doesn't need its own
    migration. See design.md's Risks / Trade-offs section.
    """

    class Mode(models.TextChoices):
        DATE_ONLY = "date_only", "Date only"
        TIME_SLOTS = "time_slots", "Time slots"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        FINALIZED = "finalized", "Finalized"
        CANCELLED = "cancelled", "Cancelled"

    id = models.CharField(
        primary_key=True, max_length=8, default=generate_short_id, editable=False
    )
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    title = models.CharField(max_length=30)
    host_nickname = models.CharField(max_length=40)
    host_email = models.EmailField(null=True, blank=True)
    mode = models.CharField(max_length=20, choices=Mode.choices)
    response_deadline = models.DateTimeField()
    location = models.CharField(max_length=200, null=True, blank=True)
    description = models.CharField(max_length=50, null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    finalized_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    final_slot = models.ForeignKey(
        Slot, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    final_note = models.CharField(max_length=200, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.title
