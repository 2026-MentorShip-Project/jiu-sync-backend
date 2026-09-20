from django.test import Client, override_settings
from rest_framework.exceptions import (
    AuthenticationFailed,
    NotAuthenticated,
    NotFound,
    PermissionDenied,
    ValidationError,
)

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


def test_handler_builds_errors_array_for_nested_list_field_validation_error():
    """③ 巢狀陣列欄位（模擬 DRF 對 many=True nested serializer 的錯誤形狀）
    → 對應的 errors 項目 field 用 "slots[<index>].<子欄位>" 路徑命名，code 依
    NESTED_SUBFIELD_CODE_OVERRIDES 依子欄位名查（"date" → "SLOT_DATE_INVALID"）。

    `{}` 代表索引 0 沒有錯誤，索引 1 的 "date" 欄位有錯誤。
    """
    exc = ValidationError({"slots": [{}, {"date": ["此為必需欄位。"]}]})

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
