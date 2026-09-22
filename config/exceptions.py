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

# DRF 內建欄位驗證（required/max_length/invalid_choice/invalid 等）自動附帶的
# `.code`，換成我們自己語意化的 code。key 是 (欄位名, DRF 原始 code)；沒有手動
# 呼叫 `validate_<field>` 自訂錯誤訊息的欄位（例如純粹靠 `max_length=`/
# `required=` 這種宣告式驗證）都得靠這張表補上 code，否則 `errors[]` 裡只會有
# DRF 自己的通用 code（"required"/"max_length" 這種），不是我們要的語意化字串。
# 手動 `raise serializers.ValidationError(..., code=...)` 的欄位（見
# `_validate_host_nickname_weighted_length` 等）已經在拋出當下就給了正確的
# code，不需要在這裡重複列。查無對應時 `.get()` 回傳 None，維持原始 DRF code
# 的 fallback（見 `_resolve_field_code`），不會讓 code 整個消失。
FIELD_CODE_OVERRIDES = {
    ("title", "required"): "TITLE_REQUIRED",
    ("title", "max_length"): "TITLE_TOO_LONG",
    ("hostNickname", "required"): "HOST_NICKNAME_REQUIRED",
    ("mode", "required"): "MODE_INVALID",
    ("mode", "invalid_choice"): "MODE_INVALID",
    ("slots", "required"): "SLOTS_REQUIRED",
    ("responseDeadline", "required"): "RESPONSE_DEADLINE_REQUIRED",
    ("location", "max_length"): "LOCATION_TOO_LONG",
    ("description", "max_length"): "DESCRIPTION_TOO_LONG",
    ("hostEmail", "invalid"): "HOST_EMAIL_INVALID",
    ("idToken", "required"): "ID_TOKEN_REQUIRED",
    ("idToken", "blank"): "ID_TOKEN_REQUIRED",
    ("nickname", "required"): "NICKNAME_REQUIRED",
    ("nickname", "blank"): "NICKNAME_REQUIRED",
    ("phoneLastThree", "required"): "PHONE_LAST_THREE_REQUIRED",
    ("phoneLastThree", "blank"): "PHONE_LAST_THREE_REQUIRED",
    ("phoneLastThree", "invalid"): "PHONE_LAST_THREE_INVALID",
    ("slotAvailabilities", "required"): "SLOT_AVAILABILITIES_REQUIRED",
    ("email", "invalid"): "PARTICIPANT_EMAIL_INVALID",
    ("email", "blank"): "PARTICIPANT_EMAIL_INVALID",
    ("comment", "max_length"): "COMMENT_TOO_LONG",
}

# 巢狀陣列欄位（`slots[].<subfield>`、`slotAvailabilities[].<subfield>`）的
# code 對照——用子欄位名比對, 不分是 DRF 的 required 還是 invalid，這個巢狀
# 層級前端只要求一個 code。
NESTED_SUBFIELD_CODE_OVERRIDES = {
    "date": "SLOT_DATE_INVALID",
    "time": "SLOT_TIME_INVALID",
    "label": "SLOT_LABEL_TOO_LONG",
    "slotId": "SLOT_ID_INVALID",
    "availability": "AVAILABILITY_INVALID",
}


def _find_leaf_detail(value):
    """遞迴往下找到第一個 leaf（不轉成字串，保留 DRF `ErrorDetail` 物件本身，
    才能同時拿到訊息文字跟 `.code`）。

    - dict → 取第一個 key 對應的值，繼續往下找;空 dict 沒有東西可取，回傳 None
    - list → 取第一個元素，繼續往下找;空 list 同理回傳 None
    - 其他 → 已經是 leaf，直接回傳

    `custom_exception_handler` 是全站共用的基礎設施，不是只服務我們自己控制的
    serializer——理論上任何手動 `raise ValidationError({"field": []})` 或
    `{"field": {}}` 這種空容器都可能被丟進來（例如未來的自訂 validator、第三方
    套件）。DRF 自己的驗證邏輯不會產生這種形狀，但這裡本來就不能假設輸入一定
    「合法」，回傳 None 讓呼叫端（`_build_errors`）略過，而不是讓 `next(iter(...))`/
    `value[0]` 對空容器直接炸 `StopIteration`/`IndexError`，把一個原本該是 400
    的驗證錯誤意外變成 500。
    """
    if isinstance(value, dict):
        if not value:
            return None
        return _find_leaf_detail(next(iter(value.values())))
    if isinstance(value, list):
        if not value:
            return None
        return _find_leaf_detail(value[0])
    return value


def _resolve_field_code(field, raw_code):
    """把 DRF 原始的 `.code`（例如 `"max_length"`）換成我們自己語意化的 code。

    查 `FIELD_CODE_OVERRIDES`；查無對應時直接沿用 DRF 原始 code 當 fallback，
    不會讓 code 整個消失變成 None——手動 `raise ValidationError(..., code=...)`
    的欄位（例如 `hostNickname` 加權長度）已經在拋出當下給了正確 code，這裡查
    不到表也只是原樣通過，行為正確。
    """
    return FIELD_CODE_OVERRIDES.get((field, raw_code), raw_code)


def _nested_indexed_items(value):
    """偵測 DRF 對 `many=True` 巢狀 serializer 產生的錯誤形狀，回傳
    `[(index, item_dict), ...]`；不是這個形狀就回傳 `None`。

    實測確認（DRF 3.18，透過真的 HTTP POST 打 `EventCreateSerializer`，不是手動
    構造 `ValidationError`）：`ListSerializer` 驗證失敗時回傳的是**只包含失敗
    索引**的 dict（例如兩筆 slots、只有索引 1 錯 → `{1: {"date": [...]}}`），
    不是補滿通過索引、長度對齊原始輸入的完整 list（`[{}, {"date": [...]}]`）。
    後者是這次改動之前這個函式唯一處理、也唯一被測試過的形狀，但那份測試是
    手動構造假資料，從來沒有透過真正的 API 請求驗證過，這個落差直到補上端對端
    測試才被抓到——`slots[0].date` 這種路徑命名先前對真實請求其實從未生效過
    （會落到下面的一般欄位分支，`field` 停在 `"slots"`、code 也拿不到
    `NESTED_SUBFIELD_CODE_OVERRIDES` 的對照）。

    這裡兩種形狀都認得:dict 形式（真實 DRF 3.18 的行為）優先；list 形式維持
    當防禦性 fallback（不確定未來 DRF 版本會不會換回這個形狀，或有沒有別的
    呼叫端手動構造這種輸入，兩種都認得比只認一種安全，成本很低）。
    """
    if isinstance(value, dict) and value and all(isinstance(k, int) for k in value):
        return sorted(value.items())
    if isinstance(value, list) and any(isinstance(item, dict) and item for item in value):
        return list(enumerate(value))
    return None


def _build_errors(data):
    """把 ValidationError 的 dict 形狀 `response.data`（沒有 "detail" 鍵）展開成
    `errors` 陣列：`[{"field": ..., "code": ..., "message": ...}, ...]`，涵蓋
    全部欄位，不只第一個。

    見 design.md D1/D2/D7。逐一走訪 `data` 的全部 key：
    - 若該值符合 `_nested_indexed_items` 認得的巢狀陣列欄位形狀（例如
      `slots`）→ 依索引展開，非空 dict 元素裡的每個子欄位各自組成一筆
      `"<key>[<index>].<子欄位>"`（只處理一層巢狀，見 design.md D2），code 用
      `NESTED_SUBFIELD_CODE_OVERRIDES` 依子欄位名查（不分 DRF 原始 code 是
      required 還是 invalid，這個巢狀層級前端只要求一個 code）
    - 否則（一般欄位、或非陣列的巢狀 dict，例如 `{"parent": {"child": [...]}}`）
      → 沿用既有的遞迴找 leaf 邏輯，取第一則訊息，code 透過 `_resolve_field_code`
      查 `FIELD_CODE_OVERRIDES`
    """
    errors = []
    for field, value in data.items():
        nested_items = _nested_indexed_items(value)
        if nested_items is not None:
            for index, item in nested_items:
                if isinstance(item, dict) and item:
                    for subfield, sub_value in item.items():
                        detail = _find_leaf_detail(sub_value)
                        if detail is None:
                            # 空容器（見 _find_leaf_detail 的說明）——沒有實際
                            # 錯誤內容可回報，略過這筆，不硬湊一個假訊息。
                            continue
                        errors.append(
                            {
                                "field": f"{field}[{index}].{subfield}",
                                "code": NESTED_SUBFIELD_CODE_OVERRIDES.get(
                                    subfield, getattr(detail, "code", None)
                                ),
                                "message": str(detail),
                            }
                        )
        else:
            detail = _find_leaf_detail(value)
            if detail is None:
                continue
            errors.append(
                {
                    "field": field,
                    "code": _resolve_field_code(field, getattr(detail, "code", None)),
                    "message": str(detail),
                }
            )
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
        # 只有明確指定 code 時才不受狀態碼查表影響（D3）；ApiError 的 code
        # 參數本身是選填的（`ApiError(message, code=None, status_code=...)`），
        # 呼叫端沒傳等於「沒有業務 code」，這種情況要落到跟其他無業務 code 的
        # 例外一樣的查表邏輯，不能因為「用了 ApiError」就整支繞過查表——否則
        # `ApiError("...", status_code=401)` 這種寫法會意外拿到 code: None，
        # 跟同一支 status_code 走 DRF 內建例外時拿到 "UNAUTHORIZED" 不一致。
        message = str(exc.detail)
        code = (
            exc.api_code
            if exc.api_code is not None
            else STATUS_CODE_DEFAULT_CODES.get(response.status_code)
        )
        errors = None
    else:
        data = response.data
        if isinstance(data, dict) and "detail" not in data:
            # DRF ValidationError 的既有 dict 形狀 → 展開成 errors 陣列。
            errors = _build_errors(data)
            # 頂層 message/code 維持是「第一個」錯誤的訊息/code，不是全部串接——
            # 保留給只讀 message/code、不讀 errors 的既有呼叫端一個向後相容的
            # 行為（design.md D1，code 比照同一邏輯延伸，見 D7）。
            message = errors[0]["message"] if errors else ""
            code = errors[0]["code"] if errors else None
        else:
            code = STATUS_CODE_DEFAULT_CODES.get(response.status_code)
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
