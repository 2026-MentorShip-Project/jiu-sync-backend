"""共用 API 錯誤格式基礎設施。

任何 view 拋出的例外，最終回應 body 都統一長 {"message": "...", "code": "..." | null}。
見 openspec/changes/api-error-format/design.md。
"""

from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler as drf_exception_handler


class ApiError(APIException):
    """view 層要帶機器可讀 `code` 時拋這個。

    `message` 進 DRF 的 `.detail`；`code` 存成 `.api_code`（不用 DRF 既有的
    `.default_code`／`get_codes()`，那組是欄位驗證用的既有機制，避免混用）。
    """

    def __init__(self, message, code=None, status_code=None):
        self.api_code = code
        if status_code is not None:
            self.status_code = status_code
        super().__init__(detail=message)


class Gone(ApiError):
    """HTTP 410 — DRF 內建沒有這個狀態碼。用法跟 `ApiError` 一樣，但狀態碼固定為 410。"""

    status_code = 410

    def __init__(self, message, code=None):
        super().__init__(message, code=code, status_code=410)


def _flatten_message(data):
    """從 DRF 預設 handler 算好的 `response.data` 裡抽出一句可讀訊息。

    - dict 且有 "detail" 鍵 → 用它
    - 字串 → 直接用
    - 其他（例如 ValidationError 的巢狀欄位字典）→ 取第一個欄位的第一條錯誤，
      格式化成 "<field>: <error>"。不保留完整結構，見 design.md Non-Goals。
    """
    if isinstance(data, dict):
        if "detail" in data:
            return str(data["detail"])
        field, errors = next(iter(data.items()))
        first_error = errors[0] if isinstance(errors, list) else errors
        return f"{field}: {first_error}"
    return str(data)


def custom_exception_handler(exc, context):
    """全站共用 EXCEPTION_HANDLER：把任何例外轉成 {"message": ..., "code": ...} 形狀。

    見 openspec/changes/api-error-format/design.md「Decisions」。
    """
    response = drf_exception_handler(exc, context)
    if response is None:
        # 不是 DRF/Django 認得的例外（例如程式本身的 bug）——放行給 Django 走
        # 原本的 500 路徑，不要在這裡插手包裝成看起來像業務錯誤。
        return None

    if isinstance(exc, ApiError):
        message = str(exc.detail)
        code = exc.api_code
    else:
        message = _flatten_message(response.data)
        code = None

    response.data = {"message": message, "code": code}
    return response
