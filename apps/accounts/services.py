import hashlib
from datetime import UTC, datetime

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from .models import RefreshTokenRecord

_VALID_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


class GoogleTokenError(Exception):
    """Raised when a Google-issued ID token fails verification."""


def verify_google_id_token(token: str) -> dict:
    """Verify a Google-issued ID token and return its claims.

    Raises GoogleTokenError if the token is invalid, expired, malformed,
    has an audience that doesn't match GOOGLE_OAUTH_CLIENT_ID, or has an
    issuer other than Google.
    """
    try:
        claims = id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            settings.GOOGLE_OAUTH_CLIENT_ID,
        )
    except ValueError as exc:
        raise GoogleTokenError(str(exc)) from exc

    if claims.get("aud") != settings.GOOGLE_OAUTH_CLIENT_ID:
        raise GoogleTokenError("Token audience does not match this application")

    if claims.get("iss") not in _VALID_ISSUERS:
        raise GoogleTokenError("Invalid token issuer")

    sub = claims.get("sub")
    if not sub:
        raise GoogleTokenError("Token is missing a stable subject identifier")

    email = claims.get("email")
    if not email:
        raise GoogleTokenError("Token is missing an email claim")
    try:
        validate_email(email)
    except ValidationError as exc:
        raise GoogleTokenError("Token email claim is not a valid email address") from exc

    if claims.get("email_verified") is not True:
        raise GoogleTokenError("Token email claim is not verified by Google")

    return claims


def _hash_refresh_token(token_value: str) -> str:
    """回傳 refresh token 字串的 SHA-256 hex digest。

    不加 salt——見 design.md「Decisions」：refresh token 是 JWT 函式庫用密碼學
    亂數產生的高熵字串，不是使用者可能重複使用的密碼，不存在彩虹表攻擊的前提。
    """
    return hashlib.sha256(token_value.encode()).hexdigest()


def record_refresh_token(user, token) -> RefreshTokenRecord:
    """登記一筆新核發（或 rotate 後新換發）的 refresh token 撤銷紀錄。

    ``token`` 是 ``rest_framework_simplejwt`` 的 ``RefreshToken`` 實例——可以
    用 mapping 介面讀出 ``jti``/``exp`` claim，也可以 ``str(token)`` 拿到原始
    JWT 字串。寫進 DB 前一定先雜湊，不對外持久化明文 token。
    """
    token_value = str(token)
    return RefreshTokenRecord.objects.create(
        jti=token["jti"],
        token_hash=_hash_refresh_token(token_value),
        user=user,
        expires_at=datetime.fromtimestamp(token["exp"], tz=UTC),
    )


def get_valid_refresh_token_record(token_value: str) -> RefreshTokenRecord | None:
    """依原始 refresh token 字串查詢對應且仍有效的 ``RefreshTokenRecord``。

    查無紀錄、已撤銷（``revoked_at`` 有值）、或已過期（``expires_at`` 是過去
    時間）都視為無效，統一回傳 ``None``——呼叫端不需要（也不應該）區分這三種
    原因，見 design.md「驗證流程順序」。查詢用雜湊值，不解 JWT。
    """
    token_hash = _hash_refresh_token(token_value)
    try:
        record = RefreshTokenRecord.objects.get(token_hash=token_hash)
    except RefreshTokenRecord.DoesNotExist:
        return None

    if record.revoked_at is not None:
        return None
    if record.expires_at <= timezone.now():
        return None
    return record


def revoke_refresh_token_record(record: RefreshTokenRecord) -> None:
    """把一筆 ``RefreshTokenRecord`` 標記為已撤銷。"""
    record.revoked_at = timezone.now()
    record.save(update_fields=["revoked_at"])
