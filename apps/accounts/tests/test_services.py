from unittest.mock import patch

import pytest
from django.test import override_settings

from apps.accounts.services import GoogleTokenError, verify_google_id_token

TEST_CLIENT_ID = "test-client-id.apps.googleusercontent.com"


def _base_claims(**overrides):
    claims = {
        "sub": "1234567890",
        "email": "host@example.com",
        "aud": TEST_CLIENT_ID,
        "iss": "accounts.google.com",
        "exp": 9999999999,
    }
    claims.update(overrides)
    return claims


@override_settings(GOOGLE_OAUTH_CLIENT_ID=TEST_CLIENT_ID)
@patch("apps.accounts.services.id_token.verify_oauth2_token")
def test_valid_claims_are_returned(mock_verify):
    """合法 claims（audience/issuer 對、未過期）→ 正常回傳 claims dict。"""
    claims = _base_claims()
    mock_verify.return_value = claims

    result = verify_google_id_token("some-valid-token")

    assert result == claims


@override_settings(GOOGLE_OAUTH_CLIENT_ID=TEST_CLIENT_ID)
@patch("apps.accounts.services.id_token.verify_oauth2_token")
def test_valid_claims_with_https_issuer_are_returned(mock_verify):
    """issuer 為 https://accounts.google.com 也應被接受。"""
    claims = _base_claims(iss="https://accounts.google.com")
    mock_verify.return_value = claims

    result = verify_google_id_token("some-valid-token")

    assert result == claims


@override_settings(GOOGLE_OAUTH_CLIENT_ID=TEST_CLIENT_ID)
@patch("apps.accounts.services.id_token.verify_oauth2_token")
def test_audience_mismatch_raises_google_token_error(mock_verify):
    """audience 跟 GOOGLE_OAUTH_CLIENT_ID 不符 → 拋出 GoogleTokenError。"""
    claims = _base_claims(aud="some-other-client-id.apps.googleusercontent.com")
    mock_verify.return_value = claims

    with pytest.raises(GoogleTokenError):
        verify_google_id_token("some-token-with-wrong-audience")


@override_settings(GOOGLE_OAUTH_CLIENT_ID=TEST_CLIENT_ID)
@patch("apps.accounts.services.id_token.verify_oauth2_token")
def test_invalid_issuer_raises_google_token_error(mock_verify):
    """issuer 不是 accounts.google.com/https://accounts.google.com → 拋出 GoogleTokenError。"""
    claims = _base_claims(iss="https://evil.example.com")
    mock_verify.return_value = claims

    with pytest.raises(GoogleTokenError):
        verify_google_id_token("some-token-with-bad-issuer")


@override_settings(GOOGLE_OAUTH_CLIENT_ID=TEST_CLIENT_ID)
@patch("apps.accounts.services.id_token.verify_oauth2_token")
def test_underlying_value_error_is_wrapped_as_google_token_error(mock_verify):
    """底層 verify_oauth2_token 拋出 ValueError（過期/格式錯）
    → 轉成 GoogleTokenError，不洩漏原始例外。
    """
    mock_verify.side_effect = ValueError("Token expired")

    with pytest.raises(GoogleTokenError):
        verify_google_id_token("some-expired-or-malformed-token")
