from unittest.mock import patch

import pytest
from django.db import IntegrityError
from rest_framework import status
from rest_framework.reverse import reverse
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.accounts.services import GoogleTokenError

pytestmark = pytest.mark.django_db

GOOGLE_LOGIN_URL = reverse("accounts:google-login")


def _base_claims(**overrides):
    claims = {
        "sub": "1234567890",
        "email": "host@example.com",
        "name": "Host Name",
        "picture": "https://example.com/avatar.png",
    }
    claims.update(overrides)
    return claims


@patch("apps.accounts.views.verify_google_id_token")
def test_valid_id_token_returns_200_with_tokens_and_creates_user(mock_verify):
    """① 合法 idToken → 200，body 含 access/refresh/user，且資料庫多一筆 User。"""
    mock_verify.return_value = _base_claims()
    client = APIClient()

    assert User.objects.count() == 0

    response = client.post(GOOGLE_LOGIN_URL, {"idToken": "valid-token"}, format="json")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert "access" in body
    assert "refresh" in body
    assert body["user"]["email"] == "host@example.com"
    assert body["user"]["name"] == "Host Name"
    assert "id" in body["user"]
    assert User.objects.count() == 1


@patch("apps.accounts.views.verify_google_id_token")
def test_repeated_login_with_same_google_sub_does_not_create_second_user(mock_verify):
    """② 同一個 google_sub 的合法 idToken 再打一次 → 不新增第二筆 User。"""
    mock_verify.return_value = _base_claims()
    client = APIClient()

    client.post(GOOGLE_LOGIN_URL, {"idToken": "valid-token"}, format="json")
    assert User.objects.count() == 1

    response = client.post(GOOGLE_LOGIN_URL, {"idToken": "valid-token"}, format="json")

    assert response.status_code == status.HTTP_200_OK
    assert User.objects.count() == 1


@patch("apps.accounts.views.verify_google_id_token")
def test_invalid_id_token_returns_401_and_creates_no_user(mock_verify):
    """③ verify_google_id_token 拋 GoogleTokenError → 401，
    body 符合 {message, code}，不建立 User。
    """
    mock_verify.side_effect = GoogleTokenError("Token expired")
    client = APIClient()

    response = client.post(GOOGLE_LOGIN_URL, {"idToken": "bad-token"}, format="json")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    body = response.json()
    assert "message" in body
    assert body["code"] == "INVALID_ID_TOKEN"
    assert User.objects.count() == 0


@patch("apps.accounts.views.verify_google_id_token")
def test_returning_user_with_changed_claims_updates_local_record(mock_verify):
    """④ 已存在的 google_sub，claims 跟本地紀錄不同時再登入 → 對應欄位被更新。"""
    client = APIClient()
    mock_verify.return_value = _base_claims()
    client.post(GOOGLE_LOGIN_URL, {"idToken": "valid-token"}, format="json")

    mock_verify.return_value = _base_claims(
        email="new-email@example.com",
        name="New Name",
        picture="https://example.com/new-avatar.png",
    )
    response = client.post(GOOGLE_LOGIN_URL, {"idToken": "valid-token-2"}, format="json")

    assert response.status_code == status.HTTP_200_OK
    assert User.objects.count() == 1
    user = User.objects.get(google_sub="1234567890")
    assert user.email == "new-email@example.com"
    assert user.display_name == "New Name"
    assert user.avatar_url == "https://example.com/new-avatar.png"


@patch("apps.accounts.views.User.objects.create")
@patch("apps.accounts.views.verify_google_id_token")
def test_concurrent_create_integrity_error_falls_back_to_get(mock_verify, mock_create):
    """⑤ 併發：User.objects.create 拋 IntegrityError →
    view 改走 get(google_sub=...)，仍 200 並核發 token。
    """
    claims = _base_claims()
    mock_verify.return_value = claims
    mock_create.side_effect = IntegrityError("duplicate key value violates unique constraint")

    # 模擬「另一個並發請求」已經先建立好這筆 User。
    existing_user = User.objects.create_user(
        email=claims["email"],
        google_sub=claims["sub"],
        display_name=claims["name"],
        avatar_url=claims["picture"],
    )

    client = APIClient()
    response = client.post(GOOGLE_LOGIN_URL, {"idToken": "valid-token"}, format="json")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert "access" in body
    assert "refresh" in body
    assert body["user"]["id"] == str(existing_user.id)
    assert User.objects.count() == 1
