from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import IntegrityError
from rest_framework import status
from rest_framework.reverse import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import RefreshTokenRecord, User
from apps.accounts.services import GoogleTokenError, record_refresh_token

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


def test_missing_id_token_returns_400_with_id_token_required_code():
    """新增:請求 body 沒帶 idToken → 400,code 為 "ID_TOKEN_REQUIRED"(依
    config/exceptions.py 的 FIELD_CODE_OVERRIDES 把 DRF 原始的 "required" code
    換成語意化字串,見 add-error-code-table 的修訂記錄)。"""
    client = APIClient()

    response = client.post(GOOGLE_LOGIN_URL, {}, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["code"] == "ID_TOKEN_REQUIRED"


@patch("apps.accounts.views.verify_google_id_token")
def test_valid_id_token_returns_200_with_access_and_user_but_not_refresh_in_body(mock_verify):
    """① 合法 idToken → 200，body 只含 access/user（不含 refresh），且資料庫多一筆 User。

    刷新憑證改走 httpOnly cookie 傳遞，body 不應再出現 refresh 欄位——見
    openspec/changes/refresh-token-httponly-cookie/specs/user-auth/spec.md
    「刷新憑證對前端程式碼不可讀取」。
    """
    mock_verify.return_value = _base_claims()
    client = APIClient()

    assert User.objects.count() == 0

    response = client.post(GOOGLE_LOGIN_URL, {"idToken": "valid-token"}, format="json")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert "access" in body
    assert body["access"]
    assert "refresh" not in body
    assert body["user"]["email"] == "host@example.com"
    assert body["user"]["name"] == "Host Name"
    assert "id" in body["user"]
    assert User.objects.count() == 1
    created_user = User.objects.get(google_sub="1234567890")
    assert created_user.has_usable_password() is False


@patch("apps.accounts.views.verify_google_id_token")
def test_valid_id_token_sets_httponly_refresh_token_cookie(mock_verify):
    """合法登入的 response 帶一個名叫 refresh_token 的 httpOnly cookie，
    且該值是一個可以拿去建構 RefreshToken 的有效 refresh token。
    """
    mock_verify.return_value = _base_claims()
    client = APIClient()

    response = client.post(GOOGLE_LOGIN_URL, {"idToken": "valid-token"}, format="json")

    assert response.status_code == status.HTTP_200_OK
    cookie = response.cookies.get("refresh_token")
    assert cookie is not None
    assert cookie["httponly"] is True
    # 不應拋例外 — 代表這是一個結構有效的 refresh token。
    RefreshToken(cookie.value)


@patch("apps.accounts.views.verify_google_id_token")
def test_valid_login_records_hashed_refresh_token_not_plaintext(mock_verify):
    """3.1① 登入成功後，資料庫的 RefreshTokenRecord 多一筆，且這筆的
    token_hash 不等於 response cookie 裡的明文 refresh token 字串——直接反查
    DB 斷言看不到明文。
    """
    mock_verify.return_value = _base_claims()
    client = APIClient()

    assert RefreshTokenRecord.objects.count() == 0

    response = client.post(GOOGLE_LOGIN_URL, {"idToken": "valid-token"}, format="json")

    assert response.status_code == status.HTTP_200_OK
    cookie_value = response.cookies["refresh_token"].value
    assert RefreshTokenRecord.objects.count() == 1
    record = RefreshTokenRecord.objects.get()
    assert record.token_hash != cookie_value
    assert cookie_value not in record.token_hash
    assert record.jti == RefreshToken(cookie_value)["jti"]
    assert record.revoked_at is None


@patch("apps.accounts.views.verify_google_id_token")
def test_refresh_with_cookie_returns_new_access_and_rotated_cookie(mock_verify):
    """同一個 APIClient 登入後（cookie 已自動記住），不帶任何 body 打
    POST /api/auth/refresh/ → 200，body 有新的 access，response 也帶一個新的
    （rotate 後的）refresh_token cookie，httponly 為真。

    3.1② 換發成功後，舊的 RefreshTokenRecord 被標記 revoked_at，且資料庫
    新增一筆新的紀錄（對應新 cookie 值）。
    """
    mock_verify.return_value = _base_claims()
    client = APIClient()
    login_response = client.post(GOOGLE_LOGIN_URL, {"idToken": "valid-token"}, format="json")
    original_refresh_cookie_value = login_response.cookies["refresh_token"].value
    original_jti = RefreshToken(original_refresh_cookie_value)["jti"]

    response = client.post(REFRESH_URL, {}, format="json")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["access"]
    assert isinstance(body["access"], str)
    new_cookie = response.cookies.get("refresh_token")
    assert new_cookie is not None
    assert new_cookie["httponly"] is True
    assert new_cookie.value != original_refresh_cookie_value

    assert RefreshTokenRecord.objects.count() == 2
    old_record = RefreshTokenRecord.objects.get(jti=original_jti)
    assert old_record.revoked_at is not None
    new_record = RefreshTokenRecord.objects.get(jti=RefreshToken(new_cookie.value)["jti"])
    assert new_record.revoked_at is None
    assert new_record.token_hash != new_cookie.value


def test_refresh_without_cookie_returns_401():
    """全新、乾淨的 APIClient（沒登入過，沒有 refresh_token cookie）打
    POST /api/auth/refresh/ → 401。
    """
    client = APIClient()

    response = client.post(REFRESH_URL, {}, format="json")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


@patch("apps.accounts.views.verify_google_id_token")
def test_refresh_with_rotated_out_cookie_returns_401(mock_verify):
    """3.1③ rotate 後舊的 refresh_token（已被標記 revoked_at）拿去打
    /api/auth/refresh/ → 401。
    """
    mock_verify.return_value = _base_claims()
    client = APIClient()
    login_response = client.post(GOOGLE_LOGIN_URL, {"idToken": "valid-token"}, format="json")
    original_refresh_cookie_value = login_response.cookies["refresh_token"].value

    # rotate 一次，讓原本的 cookie 值變成「舊的」，已被撤銷。
    client.post(REFRESH_URL, {}, format="json")

    client.cookies["refresh_token"] = original_refresh_cookie_value
    response = client.post(REFRESH_URL, {}, format="json")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    body = response.json()
    assert body["code"] == "INVALID_REFRESH_TOKEN"


def test_refresh_with_logout_revoked_cookie_returns_401():
    """3.1③ 已經被登出撤銷的 refresh_token 拿去打 /api/auth/refresh/ → 401。"""
    user = User.objects.create_user(
        email="host@example.com",
        google_sub="1234567890",
        display_name="Host Name",
        avatar_url="",
    )
    refresh = RefreshToken.for_user(user)
    record_refresh_token(user, refresh)
    access_token = str(refresh.access_token)
    refresh_token_value = str(refresh)

    client = APIClient()
    client.cookies["refresh_token"] = refresh_token_value
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")
    logout_response = client.post(LOGOUT_URL, {}, format="json")
    assert logout_response.status_code == status.HTTP_205_RESET_CONTENT

    client.cookies["refresh_token"] = refresh_token_value
    response = client.post(REFRESH_URL, {}, format="json")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    body = response.json()
    assert body["code"] == "INVALID_REFRESH_TOKEN"


def test_refresh_with_unregistered_but_structurally_valid_token_returns_401():
    """3.1④ 結構正確、簽章合法，但從沒登記過的 refresh JWT（查無 RefreshTokenRecord）
    打 /api/auth/refresh/ → 401。
    """
    user = User.objects.create_user(
        email="host@example.com",
        google_sub="1234567890",
        display_name="Host Name",
        avatar_url="",
    )
    # 刻意不呼叫 record_refresh_token，模擬「結構正確但沒登記過」。
    never_registered_refresh = str(RefreshToken.for_user(user))

    client = APIClient()
    client.cookies["refresh_token"] = never_registered_refresh
    response = client.post(REFRESH_URL, {}, format="json")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    body = response.json()
    assert body["code"] == "INVALID_REFRESH_TOKEN"


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
    assert "refresh" not in body
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


def test_logout_with_valid_refresh_cookie_returns_205_blacklists_it_and_clears_cookie():
    """① 用同一個 client 的 refresh_token cookie（不帶 body）打 logout → 205，
    該 token 對應的 RefreshTokenRecord 被標記 revoked_at
    ② response 對 refresh_token cookie 下發刪除指令（Max-Age=0）。
    """
    user = User.objects.create_user(
        email="host@example.com",
        google_sub="1234567890",
        display_name="Host Name",
        avatar_url="https://example.com/avatar.png",
    )
    refresh = RefreshToken.for_user(user)
    record_refresh_token(user, refresh)
    access_token = str(refresh.access_token)
    refresh_token = str(refresh)

    client = APIClient()
    client.cookies["refresh_token"] = refresh_token
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")

    response = client.post(LOGOUT_URL, {}, format="json")

    assert response.status_code == status.HTTP_205_RESET_CONTENT
    record = RefreshTokenRecord.objects.get(jti=refresh["jti"])
    assert record.revoked_at is not None

    deleted_cookie = response.cookies["refresh_token"]
    assert deleted_cookie.value == ""
    assert deleted_cookie["max-age"] == 0


@patch("apps.accounts.views.verify_google_id_token")
def test_refresh_then_logout_then_refresh_again_is_rejected(mock_verify):
    """整合驗證：走完整登入 → refresh → logout 撤銷 → refresh 再被拒的完整流程，
    全程依賴同一個 APIClient 自動保存／附帶 refresh_token cookie，不手動組 cookie。

    ① 用 GoogleLoginView 登入，body 拿到 access（不含 refresh，refresh 走 cookie）
    ② 不帶 body 打 /api/auth/refresh/ → 200，回傳新的 access（非空字串），
       cookie 也 rotate 成新值
    ③ 不帶 body 打 /api/auth/logout/（帶合法 access）撤銷當下 cookie 裡的 refresh，
       同時清掉這個 cookie
    ④ 撤銷後同一個 client 再打 /api/auth/refresh/（cookie 已被清掉）→ 401
    """
    mock_verify.return_value = _base_claims()
    client = APIClient()

    # ① 登入拿到 access；refresh 走 cookie，client 自動記住
    login_response = client.post(GOOGLE_LOGIN_URL, {"idToken": "valid-token"}, format="json")
    assert login_response.status_code == status.HTTP_200_OK
    login_body = login_response.json()
    original_access = login_body["access"]
    assert original_access
    assert "refresh" not in login_body

    # ② 用 cookie 帶的 refresh 換新的 access（rotation 生效：cookie 同步 rotate）
    refresh_response = client.post(REFRESH_URL, {}, format="json")
    assert refresh_response.status_code == status.HTTP_200_OK
    refresh_body = refresh_response.json()
    new_access = refresh_body["access"]
    assert new_access
    assert isinstance(new_access, str)
    assert "refresh" not in refresh_body

    # ③ 用合法 access 當 Authorization，撤銷當下 cookie 裡仍有效的 refresh（rotate 後的那組）
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {new_access}")
    logout_response = client.post(LOGOUT_URL, {}, format="json")
    assert logout_response.status_code == status.HTTP_205_RESET_CONTENT

    # ④ 撤銷後同一個 client 再打 /refresh/（cookie 已被清掉）→ 401
    client.credentials()  # 清掉 Authorization，refresh 端點本來就不需要
    rejected_response = client.post(REFRESH_URL, {}, format="json")
    assert rejected_response.status_code == status.HTTP_401_UNAUTHORIZED


def test_logout_with_another_users_refresh_cookie_returns_403_and_does_not_revoke_it():
    """模擬「cookie 被竄改」：A、B 各自登入（各自拿到自己的 refresh_token cookie），
    把 B client 的 refresh_token cookie 值讀出來，手動設進 A client 的 cookie jar，
    再用 A 的 access token 打登出 → 403 REFRESH_TOKEN_NOT_YOURS，
    且 B 的 refresh token 事後仍然有效（B 自己的 client 打 refresh 仍是 200）。

    3.1⑤ B 的 refresh token 要先經過 record_refresh_token 登記（模擬真實登入），
    這樣 LogoutView 判斷歸屬時走的才是新表查詢（record.user_id），不是舊的
    JWT payload 解析——B 沒登記過的話查表會直接視為無效，測不到「歸屬判斷」
    這件事本身。
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
    record_refresh_token(user_b, refresh_b)
    refresh_token_b = str(refresh_b)

    client_a = APIClient()
    client_a.cookies["refresh_token"] = refresh_token_b
    client_a.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token_a}")

    response = client_a.post(LOGOUT_URL, {}, format="json")

    assert response.status_code == status.HTTP_403_FORBIDDEN
    body = response.json()
    assert body["code"] == "REFRESH_TOKEN_NOT_YOURS"

    client_b = APIClient()
    client_b.cookies["refresh_token"] = refresh_token_b
    refresh_response = client_b.post(REFRESH_URL, {}, format="json")
    assert refresh_response.status_code == status.HTTP_200_OK
    assert refresh_response.json()["access"]


def test_logout_with_invalid_refresh_cookie_returns_400():
    """② 帶格式錯誤／無效的 refresh_token cookie → 400，body 符合 {message, code} 形狀。"""
    user = User.objects.create_user(
        email="host2@example.com",
        google_sub="0987654321",
        display_name="Host Name 2",
        avatar_url="https://example.com/avatar2.png",
    )
    access_token = str(RefreshToken.for_user(user).access_token)

    client = APIClient()
    client.cookies["refresh_token"] = "not-a-real-token"
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")

    response = client.post(LOGOUT_URL, {}, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    body = response.json()
    assert "message" in body
    assert body["code"] == "INVALID_REFRESH_TOKEN"


def test_logout_without_refresh_cookie_returns_400():
    """沒有 refresh_token cookie（例如已經登出過一次）打 logout → 400。"""
    user = User.objects.create_user(
        email="host3@example.com",
        google_sub="1122334455",
        display_name="Host Name 3",
        avatar_url="",
    )
    access_token = str(RefreshToken.for_user(user).access_token)

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")

    response = client.post(LOGOUT_URL, {}, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    body = response.json()
    assert body["code"] == "INVALID_REFRESH_TOKEN"
