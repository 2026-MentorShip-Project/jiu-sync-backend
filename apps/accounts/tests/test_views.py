from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import IntegrityError
from rest_framework import status
from rest_framework.reverse import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User
from apps.accounts.services import GoogleTokenError

pytestmark = pytest.mark.django_db

GOOGLE_LOGIN_URL = reverse("accounts:google-login")
ME_URL = "/api/me/"
LOGOUT_URL = reverse("accounts:logout")
REFRESH_URL = reverse("accounts:token-refresh")


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
    created_user = User.objects.get(google_sub="1234567890")
    assert created_user.has_usable_password() is False


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
    assert body["message"] != "Token expired"
    assert body["message"] == "Google 登入驗證失敗，請重新登入"


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


@patch("apps.accounts.views.verify_google_id_token")
def test_concurrent_create_integrity_error_falls_back_to_get(mock_verify):
    """⑤ 併發：User.objects.create_user 拋 IntegrityError（google_sub 撞到）→
    view 改走 get(google_sub=...)，仍 200 並核發 token。

    真實併發情境下，psycopg 會在 `exc.__cause__.diag.constraint_name` 附上實際撞到
    的 constraint 名稱，這裡用一個假的 diag 物件模擬，確保測的是「google_sub 撞到」
    這個併發情境，而不是被誤判成 email 衝突。

    `User.objects.create_user` 的 patch 只包住 `client.post(...)` 那一段（而非整個測試
    函式），因為測試本身也要用同一個 `create_user` 來模擬「另一個並發請求」已經
    先建立好的既有使用者，若用函式層級的 @patch 裝飾器會連這段既有使用者的
    建立都攔截掉。
    """
    claims = _base_claims()
    mock_verify.return_value = claims
    fake_cause = Exception("duplicate key value violates unique constraint")
    fake_cause.diag = SimpleNamespace(constraint_name="accounts_user_google_sub_key")
    fake_integrity_error = IntegrityError("duplicate key value violates unique constraint")
    fake_integrity_error.__cause__ = fake_cause

    # 模擬「另一個並發請求」已經先建立好這筆 User。
    existing_user = User.objects.create_user(
        email=claims["email"],
        google_sub=claims["sub"],
        display_name=claims["name"],
        avatar_url=claims["picture"],
    )

    client = APIClient()
    with patch(
        "apps.accounts.views.User.objects.create_user",
        side_effect=fake_integrity_error,
    ):
        response = client.post(GOOGLE_LOGIN_URL, {"idToken": "valid-token"}, format="json")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert "access" in body
    assert "refresh" in body
    assert body["user"]["id"] == str(existing_user.id)
    assert User.objects.count() == 1


@patch("apps.accounts.views.verify_google_id_token")
def test_email_conflict_with_different_google_sub_returns_409(mock_verify):
    """不同 Google 帳號的 email 撞到既有紀錄 → 409 EMAIL_ALREADY_IN_USE，
    不建立新 User，也不覆寫既有紀錄。
    """
    existing_user = User.objects.create_user(
        email="shared@example.com",
        google_sub="existing-sub",
        display_name="Existing Host",
        avatar_url="https://example.com/existing.png",
    )
    mock_verify.return_value = _base_claims(
        sub="new-sub",
        email="shared@example.com",
        name="New Host",
        picture="https://example.com/new.png",
    )
    client = APIClient()

    assert User.objects.count() == 1

    response = client.post(GOOGLE_LOGIN_URL, {"idToken": "valid-token"}, format="json")

    assert response.status_code == status.HTTP_409_CONFLICT
    body = response.json()
    assert body["code"] == "EMAIL_ALREADY_IN_USE"
    assert User.objects.count() == 1
    existing_user.refresh_from_db()
    assert existing_user.google_sub == "existing-sub"
    assert existing_user.display_name == "Existing Host"
    assert existing_user.avatar_url == "https://example.com/existing.png"


def test_me_with_valid_access_token_returns_200_with_profile_fields():
    """① 帶合法 access token → 200，回傳正確的 id/email/display_name/avatar_url/date_joined。"""
    user = User.objects.create_user(
        email="host@example.com",
        google_sub="1234567890",
        display_name="Host Name",
        avatar_url="https://example.com/avatar.png",
    )
    access_token = str(RefreshToken.for_user(user).access_token)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")

    response = client.get(ME_URL)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["id"] == str(user.id)
    assert body["email"] == "host@example.com"
    assert body["display_name"] == "Host Name"
    assert body["avatar_url"] == "https://example.com/avatar.png"
    assert "date_joined" in body


def test_me_without_token_returns_401():
    """② 不帶任何 Authorization header 打 GET /api/me/ → 401。"""
    client = APIClient()

    response = client.get(ME_URL)

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_logout_with_valid_refresh_token_returns_205_and_blacklists_it():
    """① 帶合法 refresh token → 205，且該 token 被加入 blacklist。"""
    user = User.objects.create_user(
        email="host@example.com",
        google_sub="1234567890",
        display_name="Host Name",
        avatar_url="https://example.com/avatar.png",
    )
    refresh = RefreshToken.for_user(user)
    access_token = str(refresh.access_token)
    refresh_token = str(refresh)

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")

    response = client.post(LOGOUT_URL, {"refresh": refresh_token}, format="json")

    assert response.status_code == status.HTTP_205_RESET_CONTENT
    outstanding = OutstandingToken.objects.get(jti=refresh["jti"])
    assert BlacklistedToken.objects.filter(token=outstanding).exists()

    with pytest.raises(TokenError):
        RefreshToken(refresh_token).blacklist()


@patch("apps.accounts.views.verify_google_id_token")
def test_refresh_then_logout_then_refresh_again_is_rejected(mock_verify):
    """6.1 整合驗證：走完整登入 → refresh → logout 撤銷 → refresh 再被拒的完整流程。

    ① 用 GoogleLoginView 登入拿到 access/refresh
    ② 拿 refresh 打 /api/auth/refresh/ → 200，回傳新的 access（非空字串）
    ③ 拿同一組 refresh 打 /api/auth/logout/（帶合法 access）撤銷它
    ④ 撤銷後再拿同一組 refresh 打 /api/auth/refresh/ → 401（simplejwt blacklist 內建行為）

    備註：settings 的 SIMPLE_JWT 開了 ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION，
    所以①打完 /refresh/ 後，登入時拿到的「原始」refresh 會被自動撤銷（rotation 的副作用），
    /refresh/ 回應同時會核發一組「新的」refresh。③④所説的「這組 refresh」在有 rotation 的
    情況下，指的是當下仍有效、可被拿去登出撤銷的那組，也就是②回應核發的新 refresh；
    用已經失效的原始 refresh 走③會在 view 內就被判定為已撤銷而回 400，不是本次要驗證的路徑。
    """
    mock_verify.return_value = _base_claims()
    client = APIClient()

    # ① 登入拿到 access/refresh
    login_response = client.post(GOOGLE_LOGIN_URL, {"idToken": "valid-token"}, format="json")
    assert login_response.status_code == status.HTTP_200_OK
    login_body = login_response.json()
    original_access = login_body["access"]
    original_refresh = login_body["refresh"]
    assert original_access
    assert original_refresh

    # ② 用 refresh 換新的 access（rotation 生效：同時拿到新的 refresh，原始 refresh 被自動撤銷）
    refresh_response = client.post(REFRESH_URL, {"refresh": original_refresh}, format="json")
    assert refresh_response.status_code == status.HTTP_200_OK
    refresh_body = refresh_response.json()
    new_access = refresh_body["access"]
    rotated_refresh = refresh_body["refresh"]
    assert new_access
    assert isinstance(new_access, str)
    assert rotated_refresh
    assert rotated_refresh != original_refresh

    # ③ 用合法 access 當 Authorization，撤銷當下仍有效的 refresh（rotation 後核發的那組）
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {new_access}")
    logout_response = client.post(LOGOUT_URL, {"refresh": rotated_refresh}, format="json")
    assert logout_response.status_code == status.HTTP_205_RESET_CONTENT

    # ④ 撤銷後同一組 refresh 再打 /refresh/ → 401
    client.credentials()  # 清掉 Authorization，refresh 端點本來就不需要
    rejected_response = client.post(REFRESH_URL, {"refresh": rotated_refresh}, format="json")
    assert rejected_response.status_code == status.HTTP_401_UNAUTHORIZED


def test_logout_with_another_users_refresh_token_returns_403_and_does_not_revoke_it():
    """用 A 的 access token 打 /api/auth/logout/，帶 B 的 refresh token
    → 403 REFRESH_TOKEN_NOT_YOURS，且 B 的 refresh token 事後仍然有效。
    """
    user_a = User.objects.create_user(
        email="host-a@example.com",
        google_sub="sub-a",
        display_name="Host A",
        avatar_url="",
    )
    user_b = User.objects.create_user(
        email="host-b@example.com",
        google_sub="sub-b",
        display_name="Host B",
        avatar_url="",
    )
    access_token_a = str(RefreshToken.for_user(user_a).access_token)
    refresh_b = RefreshToken.for_user(user_b)
    refresh_token_b = str(refresh_b)

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token_a}")

    response = client.post(LOGOUT_URL, {"refresh": refresh_token_b}, format="json")

    assert response.status_code == status.HTTP_403_FORBIDDEN
    body = response.json()
    assert body["code"] == "REFRESH_TOKEN_NOT_YOURS"

    client.credentials()
    refresh_response = client.post(REFRESH_URL, {"refresh": refresh_token_b}, format="json")
    assert refresh_response.status_code == status.HTTP_200_OK
    assert refresh_response.json()["access"]


def test_logout_with_invalid_refresh_token_returns_400():
    """② 帶格式錯誤／無效的 refresh → 400，body 符合 {message, code} 形狀。"""
    user = User.objects.create_user(
        email="host2@example.com",
        google_sub="0987654321",
        display_name="Host Name 2",
        avatar_url="https://example.com/avatar2.png",
    )
    access_token = str(RefreshToken.for_user(user).access_token)

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")

    response = client.post(LOGOUT_URL, {"refresh": "not-a-real-token"}, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    body = response.json()
    assert "message" in body
    assert body["code"] == "INVALID_REFRESH_TOKEN"
