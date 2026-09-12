import logging

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

logger = logging.getLogger(__name__)


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
            logger.warning("Google id_token verification failed: %s", exc)
            raise ApiError(
                "Google 登入驗證失敗，請重新登入", code="INVALID_ID_TOKEN", status_code=401
            ) from exc

        google_sub = claims["sub"]
        email = claims["email"]
        display_name = claims.get("name", "")
        avatar_url = claims.get("picture", "")

        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    email=email,
                    google_sub=google_sub,
                    display_name=display_name,
                    avatar_url=avatar_url,
                )
        except IntegrityError as exc:
            diag = getattr(exc.__cause__, "diag", None)
            constraint_name = getattr(diag, "constraint_name", None)
            if constraint_name == "accounts_user_google_sub_key":
                user = User.objects.get(google_sub=google_sub)
            else:
                # Not (unambiguously) the google_sub race we expect. Before
                # treating this as a genuine cross-account email conflict,
                # rule out the case where this *is* the same account and
                # Postgres simply happened to report the email constraint
                # first (both unique constraints can be violated at once on
                # a normal returning-user login, since email and google_sub
                # both already match the existing row) — see design.md
                # Post-review 修正 2.
                existing_user = User.objects.filter(google_sub=google_sub).first()
                if existing_user is None:
                    raise ApiError(
                        "此 email 已被另一個帳號使用",
                        code="EMAIL_ALREADY_IN_USE",
                        status_code=409,
                    ) from exc
                user = existing_user

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
            token = RefreshToken(refresh_token)
        except TokenError as exc:
            raise ApiError(str(exc), code="INVALID_REFRESH_TOKEN", status_code=400) from exc

        if str(token["user_id"]) != str(request.user.id):
            raise ApiError(
                "只能撤銷自己的刷新憑證",
                code="REFRESH_TOKEN_NOT_YOURS",
                status_code=403,
            )

        try:
            token.blacklist()
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
