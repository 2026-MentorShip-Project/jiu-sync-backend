"""純函式單元測試,不經完整 HTTP 請求——``_get_client_ip``
(add-comment-rate-limit design.md D4)。優先信任 nginx 轉發的
``X-Forwarded-For``(取第一個逗號分隔值),本機開發環境(無 nginx)退回
``REMOTE_ADDR``。"""

from django.test import RequestFactory

from apps.events.views import _get_client_ip

factory = RequestFactory()


def test_returns_first_value_from_x_forwarded_for_header():
    """① 帶 ``X-Forwarded-For: 1.2.3.4, 5.6.7.8`` 的 request → 回傳
    ``1.2.3.4``(取第一個)。"""
    request = factory.get("/", HTTP_X_FORWARDED_FOR="1.2.3.4, 5.6.7.8")

    assert _get_client_ip(request) == "1.2.3.4"


def test_falls_back_to_remote_addr_without_x_forwarded_for_header():
    """② 沒有 ``X-Forwarded-For`` 時退回 ``REMOTE_ADDR``。"""
    request = factory.get("/", REMOTE_ADDR="9.9.9.9")

    assert _get_client_ip(request) == "9.9.9.9"


def test_single_value_x_forwarded_for_header_is_used_as_is():
    """邊界:``X-Forwarded-For`` 只有單一值(沒有逗號)時,整個值就是 client
    IP。"""
    request = factory.get("/", HTTP_X_FORWARDED_FOR="1.2.3.4")

    assert _get_client_ip(request) == "1.2.3.4"
