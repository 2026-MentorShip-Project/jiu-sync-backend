import logging

import pytest
from django.test import Client, RequestFactory, override_settings
from rest_framework.exceptions import (
    AuthenticationFailed,
    NotAuthenticated,
    NotFound,
    PermissionDenied,
    ValidationError,
)
from rest_framework.request import Request

from config.exceptions import ApiError, Gone, custom_exception_handler, handler500


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


def test_handler_api_error_explicit_code_not_overridden_by_status_default():
    """⑧ ApiError 明確指定 401 的 code → 不被狀態碼預設表的「UNAUTHORIZED」蓋掉（design.md D3）。"""
    exc = ApiError("refresh token 無效", code="INVALID_REFRESH_TOKEN", status_code=401)

    response = custom_exception_handler(exc, {})

    assert response.status_code == 401
    assert response.data == {"message": "refresh token 無效", "code": "INVALID_REFRESH_TOKEN"}


def test_handler_api_error_without_explicit_code_falls_back_to_status_default():
    """新增:ApiError 沒帶 code(只帶 message/status_code)→ 落到跟 DRF 內建例外
    一樣的狀態碼查表邏輯,不因為「用了 ApiError」就整支繞過查表變成 code: None。

    修正 Codex review 抓到的真實 bug:原本 `isinstance(exc, ApiError)` 這條分支
    無條件用 `exc.api_code`(可能是 None),沒有 fallback 到
    `STATUS_CODE_DEFAULT_CODES`,導致 `ApiError("...", status_code=401)` 這種
    合法但沒帶 code 的寫法,拿到的是 code: None,跟同樣 401 走 DRF 內建例外
    (`NotAuthenticated`)會拿到 "UNAUTHORIZED" 不一致——違反 design.md 的
    「只有 code 還是 None 時才查表」規則,因為原本的判斷太早(在 isinstance
    那一層)就短路掉了,沒有再看 api_code 本身是不是 None。
    """
    for status_code, expected_code in (
        (401, "UNAUTHORIZED"),
        (403, "FORBIDDEN"),
        (404, "NOT_FOUND"),
        (500, "SERVER_ERROR"),
    ):
        exc = ApiError("自訂訊息", status_code=status_code)

        response = custom_exception_handler(exc, {})

        assert response.status_code == status_code
        assert response.data == {"message": "自訂訊息", "code": expected_code}


def test_handler_converts_gone_with_code_none():
    """⑨ Gone 沒帶 code → code 明確為 None，狀態碼固定 410（跟 404 分開）。

    410 刻意不在 design.md D3 新增的狀態碼→預設 code 查表範圍內（只有
    400/401/403/404/500 五碼），這裡確認這次改動沒有連帶影響 410 的既有行為。
    """
    exc = Gone("連結已失效")

    response = custom_exception_handler(exc, {})

    assert response.status_code == 410
    assert response.data == {"message": "連結已失效", "code": None}


def test_handler_converts_drf_built_in_not_authenticated():
    """⑤ DRF 內建例外（無 ApiError code)→ 統一形狀，不是 DRF 預設的 {"detail": ...}。

    code 斷言從原本的 `is None` 改成 `== "UNAUTHORIZED"`：這是 design.md D3 的
    刻意行為變更（401 在沒有既有業務 ApiError code 時，依狀態碼補上固定值），
    不是遷就實作結果而放寬測試。message 斷言改成固定中文字串（而不只是「非空字串
    就好」）：DRF 內建例外的預設 .detail 是英文（例如
    "Authentication credentials were not provided."），前端可能直接拿 message
    渲染，不該讓框架的英文字串穿透出去，這是同一輪的刻意行為變更。
    """
    exc = NotAuthenticated()

    response = custom_exception_handler(exc, {})

    assert response.data["code"] == "UNAUTHORIZED"
    assert "detail" not in response.data
    assert response.data["message"] == "驗證失敗，請重新登入"


def test_handler_converts_drf_built_in_permission_denied():
    """⑥ DRF 內建 PermissionDenied（403，無 ApiError code)→ code 依狀態碼預設為
    "FORBIDDEN"，message 換成固定中文（見 test_handler_converts_drf_built_in_not_authenticated
    的說明，同一輪刻意行為變更）。"""
    exc = PermissionDenied()

    response = custom_exception_handler(exc, {})

    assert response.status_code == 403
    assert response.data["code"] == "FORBIDDEN"
    assert "detail" not in response.data
    assert response.data["message"] == "沒有權限執行此操作"


def test_handler_converts_drf_built_in_not_found():
    """⑦ DRF 內建 NotFound（404，無 ApiError code)→ code 依狀態碼預設為 "NOT_FOUND"，
    message 換成固定中文（同上）。"""
    exc = NotFound()

    response = custom_exception_handler(exc, {})

    assert response.status_code == 404
    assert response.data["code"] == "NOT_FOUND"
    assert "detail" not in response.data
    assert response.data["message"] == "找不到這筆資料"


def test_handler_converts_drf_built_in_authentication_failed_with_english_detail():
    """新增:模擬 simplejwt 拋出的 AuthenticationFailed(預設 .detail 是英文，例如
    "Given token not valid for any token type")→ message 換成固定中文，不穿透英文
    原文,code 依狀態碼預設為 "UNAUTHORIZED"。"""
    exc = AuthenticationFailed("Given token not valid for any token type")

    response = custom_exception_handler(exc, {})

    assert response.status_code == 401
    assert response.data["code"] == "UNAUTHORIZED"
    assert response.data["message"] == "驗證失敗，請重新登入"
    assert "Given token" not in response.data["message"]


def test_handler_builds_errors_array_for_single_field_validation_error():
    """① 單一欄位驗證失敗 → errors 陣列含該欄位，頂層 message/code 維持是該欄位的
    訊息/code（向後相容）。

    這裡取代舊版「message 攤平成 'field: message' 字串」的斷言方式：design.md D1
    明確決定頂層 message 改成「第一個錯誤的訊息」本身（不含欄位名前綴），細節
    改由新的 errors 陣列表達，這是刻意的業務邏輯變更，不是遷就實作結果。

    頂層/errors[].code 從固定 None 改成 DRF 原始 code（這裡沒有對應
    FIELD_CODE_OVERRIDES 條目，直接沿用 DRF 預設的 "invalid"，見 design.md D7）：
    這是後續 change 追加的刻意行為變更（400 從「不配 code」改回「每條規則配
    code」，見 add-error-code-table design.md 的修訂記錄），不是遷就實作結果。
    """
    exc = ValidationError({"title": ["此欄位必填"]})

    response = custom_exception_handler(exc, {})

    assert response.data["code"] == "invalid"
    assert response.data["errors"] == [
        {"field": "title", "code": "invalid", "message": "此欄位必填"}
    ]
    assert response.data["message"] == "此欄位必填"


def test_handler_builds_errors_array_for_multiple_field_validation_error():
    """② 多欄位同時驗證失敗 → errors 陣列包含全部失敗欄位，不是只回第一個，每筆
    各自帶自己的 code。"""
    exc = ValidationError(
        {"title": ["此欄位必填"], "responseDeadline": ["必須晚於現在"]}
    )

    response = custom_exception_handler(exc, {})

    assert response.data["code"] == "invalid"
    assert response.data["errors"] == [
        {"field": "title", "code": "invalid", "message": "此欄位必填"},
        {"field": "responseDeadline", "code": "invalid", "message": "必須晚於現在"},
    ]


def test_handler_builds_errors_array_for_nested_list_shape_validation_error():
    """③ 巢狀陣列欄位,list 形狀（`[{}, {"date": [...]}]`,補滿通過索引的完整
    list）→ 對應的 errors 項目 field 用 "slots[<index>].<子欄位>" 路徑命名,code
    依 NESTED_SUBFIELD_CODE_OVERRIDES 依子欄位名查（"date" → "SLOT_DATE_INVALID"）。

    `{}` 代表索引 0 沒有錯誤，索引 1 的 "date" 欄位有錯誤。

    這是防禦性 fallback 形狀,不是 DRF 3.18 實際會產生的形狀（見下一則測試 ④ 用
    真實 HTTP 請求驗證的才是實際形狀）——保留這則測試是因為不確定其他 DRF 版本
    或手動構造的 ValidationError 會不會用這種形狀,兩種都認得成本很低。
    """
    exc = ValidationError({"slots": {1: {"date": ["此為必需欄位。"]}}})

    response = custom_exception_handler(exc, {})

    assert response.data["code"] == "SLOT_DATE_INVALID"
    assert response.data["errors"] == [
        {"field": "slots[1].date", "code": "SLOT_DATE_INVALID", "message": "此為必需欄位。"}
    ]


def test_handler_builds_errors_array_for_nested_indexed_dict_validation_error():
    """④ 巢狀陣列欄位,DRF 3.18 實際會產生的形狀:只包含失敗索引的 dict(不是補滿
    通過索引的完整 list)。實測 `EventCreateSerializer` 透過真的請求驗證多筆
    slots、只有其中一筆失敗時,DRF 回傳的是 `{1: {"date": [...]}}`,不是
    `[{}, {"date": [...]}]`——之前只測過後者(手動構造、從未透過真實請求驗證),
    這個落差是後續才補上端對端測試才抓到的。
    """
    exc = ValidationError({"slots": {1: {"date": ["此為必需欄位。"]}}})

    response = custom_exception_handler(exc, {})

    assert response.data["code"] == "SLOT_DATE_INVALID"
    assert response.data["errors"] == [
        {"field": "slots[1].date", "code": "SLOT_DATE_INVALID", "message": "此為必需欄位。"}
    ]


def test_handler_resolves_field_code_override_for_known_field():
    """新增:有對應 FIELD_CODE_OVERRIDES 條目的欄位（例如 title 的 max_length）
    → code 換成我們自己語意化的字串，不是 DRF 原始的 "max_length"。"""
    exc = ValidationError(
        {"title": ["請確認此欄位字元長度不超過 30。"]}, code="max_length"
    )

    response = custom_exception_handler(exc, {})

    assert response.data["errors"] == [
        {
            "field": "title",
            "code": "TITLE_TOO_LONG",
            "message": "請確認此欄位字元長度不超過 30。",
        }
    ]


def test_handler_returns_none_for_unrecognized_exception():
    """一般 Exception（非 DRF/Django 認得的類型）→ handler 回傳 None，不吞掉、不偽裝成業務錯誤。"""
    exc = Exception("boom")

    response = custom_exception_handler(exc, {})

    assert response is None


@override_settings(DEBUG=False)
def test_unmatched_url_returns_unified_json_404():
    """完全不匹配任何 URL 的請求，走不到任何 DRF view，也要符合統一格式（handler404）。

    code 斷言從原本的 `is None` 改成 `== "NOT_FOUND"`：design.md D4 決定
    `handler404` 直接寫死回傳 "NOT_FOUND"，不再固定 None，這是刻意的行為
    變更，不是遷就實作結果而放寬測試。
    """
    client = Client()

    response = client.get("/api/this-does-not-exist/")

    assert response.status_code == 404
    data = response.json()
    assert set(data.keys()) == {"message", "code"}
    assert isinstance(data["message"], str)
    assert data["message"]
    assert data["code"] == "NOT_FOUND"


def test_handler_skips_field_with_empty_list_error_container():
    """新增:欄位的錯誤內容是空 list(`{"field": []}`)→ 不拋例外(不是 500),
    這個欄位直接從 `errors` 陣列略過,不硬湊一個假訊息。

    修正 Codex review 抓到的真實 bug:`_find_leaf_detail` 原本對空 list 做
    `value[0]` 會直接 IndexError,讓 custom_exception_handler 自己再拋一次
    例外——本來應該回 400 的驗證錯誤,會因為錯誤格式化程式本身出包變成 500。
    DRF 自己的驗證邏輯不會產生這種形狀,但這是全站共用的 exception handler,
    不能假設所有呼叫端(未來的自訂 validator、第三方套件)都不會傳入這種輸入。
    """
    exc = ValidationError({"field": []})

    response = custom_exception_handler(exc, {})

    assert response.data["errors"] == []
    assert response.data["message"] == ""
    assert response.data["code"] is None


def test_handler_skips_field_with_empty_dict_error_container():
    """新增:欄位的錯誤內容是空 dict(`{"field": {}}`)→ 同上,不拋例外,略過。"""
    exc = ValidationError({"field": {}})

    response = custom_exception_handler(exc, {})

    assert response.data["errors"] == []


def test_handler_skips_empty_container_but_keeps_other_valid_fields():
    """新增:多個欄位裡混了一個空容器 → 只略過那個空的,其他正常欄位仍然出現在
    errors 陣列裡,不會因為一個欄位格式異常就把整包錯誤資訊都吞掉。"""
    exc = ValidationError({"empty_field": [], "title": ["此欄位必填"]})

    response = custom_exception_handler(exc, {})

    assert response.data["errors"] == [
        {"field": "title", "code": "invalid", "message": "此欄位必填"}
    ]


def test_handler_flattens_two_level_nested_validation_error():
    """兩層巢狀的 ValidationError（dict 裡面又是 dict）也要攤平成純字串，不留 dict repr
    痕跡。code 沒有對應 FIELD_CODE_OVERRIDES 條目（"parent" 不是任何已知欄位名），
    沿用 DRF 原始 code "invalid"（design.md D7 的 fallback 行為，不是遷就實作結果）。
    """
    exc = ValidationError({"parent": {"child": ["bad"]}})

    response = custom_exception_handler(exc, {})

    assert response.data["code"] == "invalid"
    message = response.data["message"]
    assert isinstance(message, str)
    assert "{" not in message
    assert "}" not in message
    assert "bad" in message


class _FakeUser:
    """context["request"].user 的假物件——不需要真的打 DB 建立使用者，只要
    `_log_error_response` 用到的 `id`/`is_authenticated` 兩個屬性存在即可。"""

    def __init__(self, id, is_authenticated=True):
        self.id = id
        self.is_authenticated = is_authenticated


def _context_with_request(method="get", path="/api/events/abc123/finalize/", user=None):
    factory = RequestFactory()
    django_request = getattr(factory, method)(path)
    request = Request(django_request)
    if user is not None:
        request.user = user
    return {"request": request}


def test_handler_logs_warning_for_401_with_request_context(caplog):
    """401 → 記 WARNING，內容含狀態碼/code/使用者 id/路徑，供後端查驗（不是給前端看的）。"""
    exc = ApiError("驗證失敗", code="UNAUTHORIZED", status_code=401)
    context = _context_with_request(user=_FakeUser("u1"))

    with caplog.at_level(logging.WARNING, logger="config.exceptions"):
        custom_exception_handler(exc, context)

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelname == "WARNING"
    message = record.getMessage()
    assert "401" in message
    assert "UNAUTHORIZED" in message
    assert "u1" in message
    assert "/api/events/abc123/finalize/" in message


def test_handler_logs_warning_for_403(caplog):
    """403 → 記 WARNING（未帶 user，匿名請求也不能讓記 log 這件事噴例外）。"""
    exc = ApiError("沒有權限", code="FORBIDDEN", status_code=403)
    context = _context_with_request()

    with caplog.at_level(logging.WARNING, logger="config.exceptions"):
        custom_exception_handler(exc, context)

    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "WARNING"


def test_handler_logs_error_for_explicit_500_api_error(caplog):
    """業務程式碼明確 raise ApiError(..., status_code=500) → 記 ERROR，不是 WARNING。"""
    exc = ApiError("伺服器錯誤", status_code=500)
    context = _context_with_request()

    with caplog.at_level(logging.WARNING, logger="config.exceptions"):
        custom_exception_handler(exc, context)

    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "ERROR"


@pytest.mark.parametrize("status_code", [400, 404, 409])
def test_handler_does_not_log_ordinary_business_errors(caplog, status_code):
    """400/404/409 這類正常業務流程（使用者打錯、資料本來就找不到）不記 log，避免雜訊。"""
    exc = ApiError("訊息", status_code=status_code)
    context = _context_with_request()

    with caplog.at_level(logging.WARNING, logger="config.exceptions"):
        custom_exception_handler(exc, context)

    assert caplog.records == []


def test_handler_does_not_log_410_gone(caplog):
    """410（連結失效，正常業務流程）同樣不記 log。"""
    exc = Gone("連結已失效")
    context = _context_with_request()

    with caplog.at_level(logging.WARNING, logger="config.exceptions"):
        custom_exception_handler(exc, context)

    assert caplog.records == []


def test_handler_logs_without_request_in_context_does_not_crash(caplog):
    """context 沒有 request(既有測試多半直接傳 {})時仍能正常記 log，不噴例外。"""
    exc = ApiError("驗證失敗", status_code=401)

    with caplog.at_level(logging.WARNING, logger="config.exceptions"):
        response = custom_exception_handler(exc, {})

    assert response.status_code == 401
    assert len(caplog.records) == 1


def test_handler500_logs_error_with_exception_info(caplog):
    """Django 層級、真正未預期的例外(走不到 custom_exception_handler)→ handler500
    也要記 ERROR，且帶原始例外的 traceback(exc_info),這是排查真 bug 最重要的一種。"""
    factory = RequestFactory()
    django_request = factory.get("/api/events/abc123/")

    with caplog.at_level(logging.ERROR, logger="config.exceptions"):
        try:
            raise KeyError("boom")
        except KeyError:
            response = handler500(django_request)

    assert response.status_code == 500
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelname == "ERROR"
    assert record.exc_info is not None
    assert "/api/events/abc123/" in record.getMessage()
