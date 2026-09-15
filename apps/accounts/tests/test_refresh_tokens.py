"""撤銷紀錄查詢／登記共用函式的測試。

見 openspec/changes/hashed-refresh-token-storage/tasks.md 2.1/2.2、design.md。
只測 apps/accounts/services.py 裡 record_refresh_token /
get_valid_refresh_token_record / revoke_refresh_token_record 這組函式本身，
不透過 view。
"""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import RefreshTokenRecord, User
from apps.accounts.services import (
    get_valid_refresh_token_record,
    record_refresh_token,
    revoke_refresh_token_record,
)

pytestmark = pytest.mark.django_db


def _create_user(**overrides):
    defaults = {
        "email": "host@example.com",
        "google_sub": "1234567890",
        "display_name": "Host Name",
        "avatar_url": "",
    }
    defaults.update(overrides)
    return User.objects.create_user(**defaults)


def test_record_then_query_same_token_string_returns_same_record():
    """① 登記一筆後，用同一個 token 字串能查回同一筆紀錄。"""
    user = _create_user()
    token = RefreshToken.for_user(user)
    token_value = str(token)

    record = record_refresh_token(user, token)
    found = get_valid_refresh_token_record(token_value)

    assert found is not None
    assert found.pk == record.pk
    assert found.jti == token["jti"]
    assert found.user_id == user.id


def test_record_stores_hash_not_plaintext():
    """② DB 裡的 token_hash 不是明文——不等於原始字串，也不包含原始字串當子字串。"""
    user = _create_user()
    token = RefreshToken.for_user(user)
    token_value = str(token)

    record = record_refresh_token(user, token)
    record.refresh_from_db()

    assert record.token_hash != token_value
    assert token_value not in record.token_hash
    assert record.token_hash not in token_value
    assert len(record.token_hash) == 64
    # 看起來像 hex digest，不是 JWT（JWT 會有兩個 "." 分隔出三段）。
    assert "." not in record.token_hash
    int(record.token_hash, 16)  # 不拋例外即代表是合法的十六進位字串


def test_revoked_record_is_invalid():
    """③ 已撤銷（revoked_at 有值）的紀錄查詢應視為無效。"""
    user = _create_user()
    token = RefreshToken.for_user(user)
    token_value = str(token)
    record = record_refresh_token(user, token)

    revoke_refresh_token_record(record)

    assert get_valid_refresh_token_record(token_value) is None
    record.refresh_from_db()
    assert record.revoked_at is not None


def test_expired_record_is_invalid():
    """④ 已過期（expires_at 是過去時間）的紀錄查詢應視為無效。"""
    user = _create_user()
    token = RefreshToken.for_user(user)
    token_value = str(token)
    record = record_refresh_token(user, token)
    RefreshTokenRecord.objects.filter(pk=record.pk).update(
        expires_at=timezone.now() - timedelta(days=1)
    )

    assert get_valid_refresh_token_record(token_value) is None


def test_unknown_token_returns_none():
    """⑤ 查無紀錄（從沒登記過的 token 字串）回傳無效。"""
    user = _create_user()
    never_recorded_token_value = str(RefreshToken.for_user(user))

    assert get_valid_refresh_token_record(never_recorded_token_value) is None
