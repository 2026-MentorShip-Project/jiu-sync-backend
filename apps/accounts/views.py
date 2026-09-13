import logging

from django.conf import settings
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

REFRESH_TOKEN_COOKIE_NAME = "refresh_token"
REFRESH_TOKEN_COOKIE_PATH = "/api/auth/"


def _set_refresh_token_cookie(response, refresh_token_value):
    """在 response 上核發（或重新核發）refresh_token cookie。

    屬性依 openspec/changes/refresh-token-httponly-cookie/design.md「Cookie 屬性」：
    HttpOnly、Secure、SameSite=None、Path=/api/auth/，Max-Age 對齊
    SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"]。登入核發、refresh rotate 後重新核發都呼叫這個。
    """
    max_age = int(settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds())
    response.set_cookie(
        REFRESH_TOKEN_COOKIE_NAME,
        refresh_token_value,
        max_age=max_age,
        httponly=True,
        secure=True,
        samesite="None",
        path=REFRESH_TOKEN_COOKIE_PATH,
    )


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

        response = Response(
            {
                "access": str(refresh.access_token),
                "user": {
                    "id": str(user.id),
                    "name": user.display_name,
                    "email": user.email,
                },
            }
        )
        _set_refresh_token_cookie(response, str(refresh))
        return response


class RefreshView(APIView):
    """``POST /api/auth/refresh/`` — 用 cookie 帶的 refresh token 換發新的 access token。

    取代 simplejwt 內建的 ``TokenRefreshView``（只認 body），改讀
    ``request.COOKIES["refresh_token"]``。因為 ``ROTATE_REFRESH_TOKENS=True``，換發時
    同步 rotate refresh 本身並重新 ``Set-Cookie``——rotate 的具體作法（``blacklist()`` +
    ``set_jti()`` + ``set_exp()`` + ``set_iat()`` + ``outstand()``）比照
    ``rest_framework_simplejwt.serializers.TokenRefreshSerializer.validate`` 在
    djangorestframework-simplejwt 5.5.1 的實作，見
    openspec/changes/refresh-token-httponly-cookie/design.md。
    """

    permission_classes = [AllowAny]

    def post(self, request):
        refresh_token_value = request.COOKIES.get(REFRESH_TOKEN_COOKIE_NAME)
        if not refresh_token_value:
            raise ApiError(
                "缺少刷新憑證，請重新登入", code="INVALID_REFRESH_TOKEN", status_code=401
            )

        try:
            token = RefreshToken(refresh_token_value)
        except TokenError as exc:
            raise ApiError(str(exc), code="INVALID_REFRESH_TOKEN", status_code=401) from exc

        access = str(token.access_token)

        if settings.SIMPLE_JWT["BLACKLIST_AFTER_ROTATION"]:
            try:
                token.blacklist()
            except AttributeError:
                pass

        token.set_jti()
        token.set_exp()
        token.set_iat()
        token.outstand()

        response = Response({"access": access})
        _set_refresh_token_cookie(response, str(token))
        return response


class LogoutView(APIView):
    """``POST /api/auth/logout/`` — 撤銷（blacklist）主揪的 refresh token，結束 session。

    見 design.md「端點形狀」與
    openspec/changes/google-sso-login/specs/user-auth/spec.md「登出撤銷 Session」。
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        refresh_token = request.COOKIES.get(REFRESH_TOKEN_COOKIE_NAME)

        if not refresh_token:
            raise ApiError(
                "缺少刷新憑證", code="INVALID_REFRESH_TOKEN", status_code=400
            )

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

        response = Response(status=205)
        response.delete_cookie(REFRESH_TOKEN_COOKIE_NAME, path=REFRESH_TOKEN_COOKIE_PATH)
        return response


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
