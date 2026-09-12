from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import GoogleLoginView, LogoutView

app_name = "accounts"

router = DefaultRouter()

urlpatterns = router.urls + [
    path("google/", GoogleLoginView.as_view(), name="google-login"),
    path("logout/", LogoutView.as_view(), name="logout"),
]
