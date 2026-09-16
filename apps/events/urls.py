from django.urls import path, register_converter

from .views import EventCreateView, EventDetailView

app_name = "events"


class ShortIdConverter:
    """Matches ``Event.id`` — an 8-char base62 string (``ids.generate_short_id``).

    Registered as ``<shortid:...>`` below, mirroring the precision of
    Django's built-in ``<uuid:...>`` converter this replaces: a path that
    doesn't match this exact shape 404s at the routing layer (path not
    found) rather than reaching the view, same as ``<uuid:...>`` did before
    the id format changed. See design.md D1 (amended).
    """

    regex = "[0-9A-Za-z]{8}"

    def to_python(self, value):
        return value

    def to_url(self, value):
        return value


register_converter(ShortIdConverter, "shortid")

urlpatterns = [
    path("", EventCreateView.as_view(), name="event-create"),
    path("<shortid:id>/", EventDetailView.as_view(), name="event-detail"),
]
