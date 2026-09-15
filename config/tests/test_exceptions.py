from django.test import Client, override_settings
from rest_framework.exceptions import NotAuthenticated, ValidationError

from config.exceptions import ApiError, Gone, custom_exception_handler


def test_api_error_with_code_and_status_code():
    """帶 code/status_code 時，.detail/.api_code/.status_code 都要照給定值。"""
    exc = ApiError("訊息", code="SOME_CODE", status_code=400)

    assert str(exc.detail) == "訊息"
    assert exc.api_code == "SOME_CODE"
    assert exc.status_code == 400


def test_api_error_without_code_and_status_code_uses_defaults():
    """不帶 code/status_code 時，api_code 為 None，status_code 用 APIException 預設值。"""
    exc = ApiError("訊息")

    assert exc.api_code is None
    assert exc.status_code == ApiError.status_code


def test_gone_status_code_is_410():
    """Gone 例外固定回 410，DRF 內建沒有這個狀態碼。"""
    exc = Gone("連結已失效")

    assert exc.status_code == 410


def test_handler_converts_api_error_to_message_code_shape():
    """ApiError 帶 code/status_code → response.data 變成 {message, code}，狀態碼照給定值。"""
    exc = ApiError("找不到這筆資料", code="NOT_FOUND_X", status_code=404)

    response = custom_exception_handler(exc, {})

    assert response.status_code == 404
    assert response.data == {"message": "找不到這筆資料", "code": "NOT_FOUND_X"}


def test_handler_converts_gone_with_code_none():
    """Gone 沒帶 code → code 明確為 None，狀態碼固定 410（跟 404 分開）。"""
    exc = Gone("連結已失效")

    response = custom_exception_handler(exc, {})

    assert response.status_code == 410
    assert response.data == {"message": "連結已失效", "code": None}


def test_handler_converts_drf_built_in_not_authenticated():
    """DRF 內建例外（無 code 概念）→ 統一形狀，code 為 None，不是 DRF 預設的 {"detail": ...}。"""
    exc = NotAuthenticated()

    response = custom_exception_handler(exc, {})

    assert response.data["code"] is None
    assert "detail" not in response.data
    assert isinstance(response.data["message"], str)
    assert response.data["message"]


def test_handler_flattens_validation_error_to_single_message():
    """ValidationError 的巢狀欄位字典 → 攤平成單一可讀字串，code 仍為 None。"""
    exc = ValidationError({"title": ["此欄位必填"]})

    response = custom_exception_handler(exc, {})

    assert response.data["code"] is None
    assert isinstance(response.data["message"], str)
    assert "title" in response.data["message"]
    assert "此欄位必填" in response.data["message"]


def test_handler_returns_none_for_unrecognized_exception():
    """一般 Exception（非 DRF/Django 認得的類型）→ handler 回傳 None，不吞掉、不偽裝成業務錯誤。"""
    exc = Exception("boom")

    response = custom_exception_handler(exc, {})

    assert response is None


@override_settings(DEBUG=False)
def test_unmatched_url_returns_unified_json_404():
    """完全不匹配任何 URL 的請求，走不到任何 DRF view，也要符合統一格式（handler404）。"""
    client = Client()

    response = client.get("/api/this-does-not-exist/")

    assert response.status_code == 404
    data = response.json()
    assert set(data.keys()) == {"message", "code"}
    assert isinstance(data["message"], str)
    assert data["message"]
    assert data["code"] is None


def test_handler_flattens_two_level_nested_validation_error():
    """兩層巢狀的 ValidationError（dict 裡面又是 dict）也要攤平成純字串，不留 dict repr 痕跡。"""
    exc = ValidationError({"parent": {"child": ["bad"]}})

    response = custom_exception_handler(exc, {})

    assert response.data["code"] is None
    message = response.data["message"]
    assert isinstance(message, str)
    assert "{" not in message
    assert "}" not in message
    assert "bad" in message
