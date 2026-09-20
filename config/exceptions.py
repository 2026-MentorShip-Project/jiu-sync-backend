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


# 沒有既有業務 ApiError 指定 code 時，依 HTTP 狀態碼補上的預設 code（design.md D3）。
# 只涵蓋這五碼；不在表裡的狀態碼（例如 410，只會透過 ApiError/Gone 走另一條分支）
# `.get()` 回傳 None 即可，不需要額外防呆。
STATUS_CODE_DEFAULT_CODES = {
    400: None,
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    500: "SERVER_ERROR",
}

# 同一批「無既有業務 ApiError」的例外（DRF/simplejwt 內建的 NotAuthenticated、
# PermissionDenied、NotFound 等），預設 .detail 都是英文，且從未被我們自己的
# 程式碼改寫過。只在 `custom_exception_handler` 的 "detail" 形狀分支套用——
# ValidationError 的欄位訊息（errors 陣列）已經是我們自己的中文文案，不受影響。
# 400 不在表裡：這個分支理論上可能出現的 400（例如非 dict 形狀的 ValidationError）
# 極罕見，沿用既有的 `_flatten_message` 行為即可，不強行塞中文。
STATUS_CODE_DEFAULT_MESSAGES = {
    401: "驗證失敗，請重新登入",
    403: "沒有權限執行此操作",
    404: "找不到這筆資料",
    500: "伺服器發生未預期的錯誤",
}


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


def _build_errors(data):
    """把 ValidationError 的 dict 形狀 `response.data`（沒有 "detail" 鍵）展開成
    `errors` 陣列：`[{"field": ..., "message": ...}, ...]`，涵蓋全部欄位，不只第一個。

    見 design.md D1/D2。逐一走訪 `data` 的全部 key：
    - 若該值是 list、且其中有非空 dict 元素（DRF 對 `many=True` 巢狀 serializer
      產生的錯誤形狀，例如 `slots`）→ 依索引展開，非空 dict 元素裡的每個子欄位各
      自組成一筆 `"<key>[<index>].<子欄位>"`（只處理一層巢狀，見 design.md D2）
    - 否則（一般欄位、或非 list 的巢狀 dict，例如 `{"parent": {"child": [...]}}`）
      → 沿用既有的遞迴找 leaf 邏輯（`_find_leaf`），取第一則訊息
    """
    errors = []
    for field, value in data.items():
        if isinstance(value, list) and any(isinstance(item, dict) and item for item in value):
            for index, item in enumerate(value):
                if isinstance(item, dict) and item:
                    for subfield, sub_value in item.items():
                        errors.append(
                            {
                                "field": f"{field}[{index}].{subfield}",
                                "message": _find_leaf(sub_value),
                            }
                        )
        else:
            errors.append({"field": field, "message": _find_leaf(value)})
    return errors


def _flatten_message(data):
    """從 DRF 預設 handler 算好的 `response.data` 裡抽出一句可讀訊息。

    只處理 `{"detail": ...}` 形狀與純字串——dict 且沒有 "detail" 鍵的
    ValidationError 形狀改由 `_build_errors` 展開成 `errors` 陣列，
    不會呼叫到這裡，見 `custom_exception_handler`。
    """
    if isinstance(data, dict):
        return str(data["detail"])
    return str(data)


def custom_exception_handler(exc, context):
    """全站共用 EXCEPTION_HANDLER：把任何例外轉成 {"message": ..., "code": ...} 形狀。

    ValidationError 的 dict 形狀（沒有 "detail" 鍵）額外帶上 `errors` 陣列，
    見 design.md「Decisions」D1/D2。401/403/404/500 在沒有既有業務 `ApiError` code
    時，依狀態碼補上固定預設值，見 D3。同一批「detail」形狀的例外（DRF/simplejwt
    內建、預設訊息是英文的）也一併換成固定中文 message，前端可以直接顯示，不會
    混到框架自帶的英文字串。
    """
    response = drf_exception_handler(exc, context)
    if response is None:
        # 不是 DRF/Django 認得的例外（例如程式本身的 bug）——放行給 Django 走
        # 原本的 500 路徑，不要在這裡插手包裝成看起來像業務錯誤。
        return None

    if isinstance(exc, ApiError):
        # 已經透過 ApiError 明確指定 code，完全不受狀態碼查表影響（D3）。
        message = str(exc.detail)
        code = exc.api_code
        errors = None
    else:
        data = response.data
        code = STATUS_CODE_DEFAULT_CODES.get(response.status_code)
        if isinstance(data, dict) and "detail" not in data:
            # DRF ValidationError 的既有 dict 形狀 → 展開成 errors 陣列。
            errors = _build_errors(data)
            # 頂層 message 維持是「第一個」錯誤的訊息，不是全部串接——保留給只讀
            # message、不讀 errors 的既有呼叫端一個向後相容的行為（design.md D1）。
            message = errors[0]["message"] if errors else ""
        else:
            errors = None
            message = STATUS_CODE_DEFAULT_MESSAGES.get(
                response.status_code, _flatten_message(data)
            )

    body = {"message": message, "code": code}
    if errors is not None:
        body["errors"] = errors
    response.data = body
    return response


def handler404(request, exception):
    """Django URL resolver 層級的 404（走不到任何 view）——見 design.md Post-review 補充決策。

    只在 `config.urls` 模組層級透過 `handler404 = "config.exceptions.handler404"`
    被 Django 引用，簽名是 Django 規定的 `(request, exception)`。
    """
    return JsonResponse(
        {"message": "找不到這個路徑", "code": STATUS_CODE_DEFAULT_CODES[404]}, status=404
    )


def handler500(request):
    """Django 層級的 500（連 DRF 例外處理都攔不到）——見 design.md Post-review 補充決策。

    `message` 是寫死的固定字串，不能包含 `exception` 細節，避免洩漏內部資訊。
    Django 自己的錯誤紀錄機制（`django.request` logger、錯誤通知）完全不受影響，
    這裡只改變回給使用者的 body 形狀。簽名是 Django 規定的 `(request)`，沒有
    `exception` 參數。
    """
    return JsonResponse(
        {"message": "伺服器發生未預期的錯誤", "code": STATUS_CODE_DEFAULT_CODES[500]}, status=500
    )
