"""純函式單元測試,不經 HTTP——``_encode_comment_cursor``/``_decode_comment_cursor``
（add-comment-pagination design.md D2）。cursor 格式為
``{created_at.isoformat()}_{id}``,view 端自己組/解析,不做 base64 包裝。"""

from types import SimpleNamespace

from django.utils import timezone

from apps.events.views import _decode_comment_cursor, _encode_comment_cursor


def test_encode_then_decode_roundtrips_to_same_values():
    """① 一組 (created_at, id) 編碼後可以解碼還原成同樣的值。"""
    created_at = timezone.now()
    fake_comment = SimpleNamespace(created_at=created_at, id="abc12345")

    cursor = _encode_comment_cursor(fake_comment)
    decoded = _decode_comment_cursor(cursor)

    assert decoded == (created_at, "abc12345")


def test_decode_invalid_format_returns_none_without_raising():
    """② 解碼一個格式不合法的字串(例如缺底線、created_at 不是合法 ISO
    格式)回傳 None,不拋例外。"""
    assert _decode_comment_cursor("no-underscore-here") is None
    assert _decode_comment_cursor("not-a-valid-datetime_abc12345") is None
    assert _decode_comment_cursor("") is None
    assert _decode_comment_cursor(None) is None


def test_decode_cursor_with_empty_id_segment_returns_none():
    """邊界:created_at 合法但 id 段落為空字串(cursor 以底線結尾)→ None。"""
    created_at = timezone.now()
    fake_comment = SimpleNamespace(created_at=created_at, id="")

    cursor = _encode_comment_cursor(fake_comment)

    assert _decode_comment_cursor(cursor) is None
