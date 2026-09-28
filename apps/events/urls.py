from django.urls import path, register_converter

from apps.recommendations.views import RestaurantRecommendationView

from .views import (
    CommentDetailView,
    CommentListCreateView,
    EventCancelView,
    EventCreateView,
    EventDetailView,
    EventFinalizeView,
    EventPollView,
    EventReopenView,
    ParticipantResponseCreateView,
    ParticipantResponseDetailView,
    ParticipantResponseVerifyView,
)

app_name = "events"


class ShortIdConverter:
    """Matches ``Event.id`` — an 8-char base62 string (``ids.generate_short_id``).

    A path that doesn't match this exact shape 404s at the routing layer
    rather than reaching the view.
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
    path(
        "<shortid:id>/responses/",
        ParticipantResponseCreateView.as_view(),
        name="participant-response-create",
    ),
    path(
        "<shortid:id>/responses/verify/",
        ParticipantResponseVerifyView.as_view(),
        name="participant-response-verify",
    ),
    path(
        "<shortid:id>/responses/<shortid:responseId>/",
        ParticipantResponseDetailView.as_view(),
        name="participant-response-detail",
    ),
    path(
        "<shortid:id>/comments/",
        CommentListCreateView.as_view(),
        name="comment-list-create",
    ),
    path(
        "<shortid:id>/comments/<shortid:commentId>/",
        CommentDetailView.as_view(),
        name="comment-detail",
    ),
    path(
        "<shortid:id>/finalize/",
        EventFinalizeView.as_view(),
        name="event-finalize",
    ),
    path(
        "<shortid:id>/cancel/",
        EventCancelView.as_view(),
        name="event-cancel",
    ),
    path(
        "<shortid:id>/reopen/",
        EventReopenView.as_view(),
        name="event-reopen",
    ),
    path(
        "<shortid:id>/restaurant-recommendations/",
        RestaurantRecommendationView.as_view(),
        name="restaurant-recommendations",
    ),
    path(
        "<shortid:id>/poll/",
        EventPollView.as_view(),
        name="event-poll",
    ),
]
