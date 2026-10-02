"""登入 / 登出的事件 log(add-observability-stack design.md D11、tasks.md 4.3)。

只以 user_id 識別,不含 email、顯示名稱或 token。
"""

import json
import logging
from unittest.mock import patch

import pytest
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User
from apps.accounts.services import GoogleTokenError, record_refresh_token

from .test_views import GOOGLE_LOGIN_URL, LOGOUT_URL, _base_claims

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _info_level(caplog):
    caplog.set_level(logging.INFO, logger="apps")


def _events(caplog, name):
    return [r for r in caplog.records if getattr(r, "event", None) == name]


def _assert_no_sensitive_data(record, *values):
    dumped = json.dumps(
        {"message": record.getMessage(), **{k: str(v) for k, v in record.__dict__.items()}},
        ensure_ascii=False,
    )
    for value in ("host@example.com", "Host Name", *values):
        assert value not in dumped


@patch("apps.accounts.views.verify_google_id_token")
def test_first_login_logs_login_succeeded_as_new_user(
    mock_verify, caplog, django_capture_on_commit_callbacks
):
    mock_verify.return_value = _base_claims()

    with django_capture_on_commit_callbacks(execute=True):
        response = APIClient().post(GOOGLE_LOGIN_URL, {"idToken": "valid-token"}, format="json")

    assert response.status_code == status.HTTP_200_OK
    records = _events(caplog, "auth.login_succeeded")
    assert len(records) == 1
    assert records[0].user_id == str(User.objects.get().id)
    assert records[0].is_new_user is True
    _assert_no_sensitive_data(records[0], response.json()["access"])


@patch("apps.accounts.views.verify_google_id_token")
def test_returning_login_logs_is_new_user_false(
    mock_verify, caplog, django_capture_on_commit_callbacks
):
    mock_verify.return_value = _base_claims()
    client = APIClient()
    client.post(GOOGLE_LOGIN_URL, {"idToken": "valid-token"}, format="json")
    caplog.clear()

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(GOOGLE_LOGIN_URL, {"idToken": "valid-token"}, format="json")

    assert response.status_code == status.HTTP_200_OK
    records = _events(caplog, "auth.login_succeeded")
    assert len(records) == 1
    assert records[0].is_new_user is False


@patch("apps.accounts.views.verify_google_id_token")
def test_invalid_token_logs_login_failed_warning_and_no_success(
    mock_verify, caplog, django_capture_on_commit_callbacks
):
    mock_verify.side_effect = GoogleTokenError("bad audience")

    with django_capture_on_commit_callbacks(execute=True):
        response = APIClient().post(GOOGLE_LOGIN_URL, {"idToken": "bad-token"}, format="json")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    failed = _events(caplog, "auth.login_failed")
    assert len(failed) == 1
    assert failed[0].levelno == logging.WARNING
    assert "bad-token" not in failed[0].getMessage()
    assert _events(caplog, "auth.login_succeeded") == []


@patch("apps.accounts.views.verify_google_id_token")
def test_login_failed_log_does_not_echo_token_from_error_message(mock_verify, caplog):
    # code-review 抓到:google-auth 對格式錯誤的 token 會拋
    # "Wrong number of segments in token: b'<token>'",原樣記錄等於把 token 印進 log;
    # 非 eyJ 開頭的 token 也不會被 Alloy 的 JWT 遮蔽規則擋下(design.md D11)。
    mock_verify.side_effect = GoogleTokenError(
        "Wrong number of segments in token: b'secret-token.abc'"
    )

    APIClient().post(GOOGLE_LOGIN_URL, {"idToken": "secret-token.abc"}, format="json")

    failed = _events(caplog, "auth.login_failed")
    assert len(failed) == 1
    _assert_no_sensitive_data(failed[0], "secret-token")


@patch("apps.accounts.views.verify_google_id_token")
def test_email_conflict_does_not_log_login_succeeded(
    mock_verify, caplog, django_capture_on_commit_callbacks
):
    User.objects.create_user(
        email="host@example.com", google_sub="other-sub", display_name="Other", avatar_url=""
    )
    mock_verify.return_value = _base_claims()

    with django_capture_on_commit_callbacks(execute=True):
        response = APIClient().post(GOOGLE_LOGIN_URL, {"idToken": "valid-token"}, format="json")

    assert response.status_code == status.HTTP_409_CONFLICT
    assert _events(caplog, "auth.login_succeeded") == []


def test_logout_logs_auth_logout(caplog, django_capture_on_commit_callbacks):
    user = User.objects.create_user(
        email="host@example.com", google_sub="sub-1", display_name="Host Name", avatar_url=""
    )
    refresh = RefreshToken.for_user(user)
    record_refresh_token(user, refresh)
    client = APIClient()
    client.cookies["refresh_token"] = str(refresh)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(LOGOUT_URL, {}, format="json")

    assert response.status_code == status.HTTP_205_RESET_CONTENT
    records = _events(caplog, "auth.logout")
    assert len(records) == 1
    assert records[0].user_id == str(user.id)
    _assert_no_sensitive_data(records[0], str(refresh))


def test_logout_without_cookie_does_not_log(caplog, django_capture_on_commit_callbacks):
    user = User.objects.create_user(
        email="host@example.com", google_sub="sub-1", display_name="Host Name", avatar_url=""
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(LOGOUT_URL, {}, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert _events(caplog, "auth.logout") == []
