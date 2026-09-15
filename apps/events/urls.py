from django.urls import path

from .views import EventCreateView, EventDetailView

app_name = "events"

urlpatterns = [
    path("", EventCreateView.as_view(), name="event-create"),
    path("<uuid:id>/", EventDetailView.as_view(), name="event-detail"),
]
