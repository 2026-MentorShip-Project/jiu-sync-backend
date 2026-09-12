from django.db import IntegrityError, transaction
from rest_framework import generics
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from config.exceptions import ApiError

from .models import User
from .serializers import GoogleLoginSerializer, UserSerializer
from .services import GoogleTokenError, verify_google_id_token


class GoogleLoginView(APIView):
    """``POST /api/auth/google/`` — 驗證 Google id_token，get-or-create 主揪，核發本站 JWT。

    見 design.md「端點形狀」與 openspec/changes/google-sso-login/specs/user-auth/spec.md。
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = GoogleLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            claims = verify_google_id_token(serializer.validated_data["idToken"])
        except GoogleTokenError as exc:
            raise ApiError(str(exc), code="INVALID_ID_TOKEN", status_code=401) from exc

        google_sub = claims["sub"]
        email = claims["email"]
        display_name = claims.get("name", "")
        avatar_url = claims.get("picture", "")

        try:
            with transaction.atomic():
                user = User.objects.create(
                    google_sub=google_sub,
                    email=email,
                    display_name=display_name,
                    avatar_url=avatar_url,
                )
        except IntegrityError:
            user = User.objects.get(google_sub=google_sub)

        user.email = email
        user.display_name = display_name
        user.avatar_url = avatar_url
        user.save()

        refresh = RefreshToken.for_user(user)

        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": {
                    "id": str(user.id),
                    "name": user.display_name,
                    "email": user.email,
                },
            }
        )


class LogoutView(APIView):
    """``POST /api/auth/logout/`` — 撤銷（blacklist）主揪的 refresh token，結束 session。

    見 design.md「端點形狀」與
    openspec/changes/google-sso-login/specs/user-auth/spec.md「登出撤銷 Session」。
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        refresh_token = request.data.get("refresh")

        try:
            RefreshToken(refresh_token).blacklist()
        except TokenError as exc:
            raise ApiError(str(exc), code="INVALID_REFRESH_TOKEN", status_code=400) from exc

        return Response(status=205)


class MeView(generics.RetrieveAPIView):
    """``GET /api/me/`` — 已登入主揪查詢自己的身份資料。

    掛在 config/urls.py 的根路徑（非 apps/accounts/urls.py 的 `/api/auth/` 前綴），
    對齊前端既有 Swagger 契約。見 design.md「端點形狀」與
    openspec/changes/google-sso-login/specs/user-auth/spec.md「已登入主揪的身份查詢」。
    """

    permission_classes = [IsAuthenticated]
    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user
