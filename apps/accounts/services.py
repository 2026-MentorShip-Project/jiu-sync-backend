from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

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
