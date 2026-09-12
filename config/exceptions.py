"""共用 API 錯誤格式基礎設施。

任何 view 拋出的例外，最終回應 body 都統一長 {"message": "...", "code": "..." | null}。
見 openspec/changes/api-error-format/design.md。
"""

from django.http import JsonResponse
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


def _find_leaf(value):
    """遞迴往下找到第一個字串 leaf，直到不再是 dict/list 為止。

    - dict → 取第一個 key 對應的值，繼續往下找
    - list → 取第一個元素，繼續往下找
    - 其他 → 已經是 leaf，轉成字串回傳
    """
    if isinstance(value, dict):
        return _find_leaf(next(iter(value.values())))
    if isinstance(value, list):
        return _find_leaf(value[0])
    return str(value)


def _flatten_message(data):
    """從 DRF 預設 handler 算好的 `response.data` 裡抽出一句可讀訊息。

    - dict 且有 "detail" 鍵 → 用它
    - 字串 → 直接用
    - 其他（例如 ValidationError 的巢狀欄位字典，可能巢狀多層）→ 取第一個欄位，
      遞迴往下找到第一個字串 leaf，格式化成 "<field>: <leaf>"。不保留完整結構，
      見 design.md Non-Goals。
    """
    if isinstance(data, dict):
        if "detail" in data:
            return str(data["detail"])
        field, errors = next(iter(data.items()))
        return f"{field}: {_find_leaf(errors)}"
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


def handler404(request, exception):
    """Django URL resolver 層級的 404（走不到任何 view）——見 design.md Post-review 補充決策。

    只在 `config.urls` 模組層級透過 `handler404 = "config.exceptions.handler404"`
    被 Django 引用，簽名是 Django 規定的 `(request, exception)`。
    """
    return JsonResponse({"message": "找不到這個路徑", "code": None}, status=404)


def handler500(request):
    """Django 層級的 500（連 DRF 例外處理都攔不到）——見 design.md Post-review 補充決策。

    `message` 是寫死的固定字串，不能包含 `exception` 細節，避免洩漏內部資訊。
    Django 自己的錯誤紀錄機制（`django.request` logger、錯誤通知）完全不受影響，
    這裡只改變回給使用者的 body 形狀。簽名是 Django 規定的 `(request)`，沒有
    `exception` 參數。
    """
    return JsonResponse({"message": "伺服器發生未預期的錯誤", "code": None}, status=500)
