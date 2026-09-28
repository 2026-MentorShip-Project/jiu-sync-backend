"""推薦引擎的共同介面、輸入/輸出型別與例外階層(design.md D7)。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RecommendationContext:
    """送給引擎的條件。``preferences`` 即解析後的 ``resolvedPreferences``(D9),
    也是 DB ``preferences`` 欄位保存的內容。"""

    preferences: dict


@dataclass(frozen=True)
class RecommendationResult:
    """引擎成功回傳。``restaurants`` 已過濾、截斷、給好 ``id`` 並轉成 camelCase(D8);
    ``model`` 為上游回應中實際使用的模型。"""

    restaurants: list
    notes: str | None
    usage: dict | None
    model: str


class EngineError(Exception):
    """引擎可預期的失敗。

    訊息(``str(exc)``/``repr(exc)``)只能是自行撰寫的摘要,**不得**夾帶上游回應、
    prompt 或使用者輸入——log formatter 會原樣輸出 ``exc_message``/``traceback``
    (D7/D11)。上游原始內容只放在 ``raw_detail``,由 view 截斷後寫進 DB
    ``error_detail``。
    """

    error_code = "UPSTREAM_INVALID_RESPONSE"
    default_message = "recommendation engine error"

    def __init__(self, message=None, *, raw_detail=None):
        super().__init__(message or self.default_message)
        self.raw_detail = raw_detail


class UpstreamTimeout(EngineError):
    error_code = "UPSTREAM_TIMEOUT"
    default_message = "upstream timeout"


class UpstreamHTTPError(EngineError):
    error_code = "UPSTREAM_HTTP_ERROR"

    def __init__(self, status, *, raw_detail=None):
        super().__init__(f"upstream HTTP {status}", raw_detail=raw_detail)
        self.status = status


class UpstreamInvalidResponse(EngineError):
    error_code = "UPSTREAM_INVALID_RESPONSE"
    default_message = "upstream invalid response"


class NoUsableResults(EngineError):
    """上游正常回應,但沒有任何同時具備 name/address 的餐廳。``notes`` 為模型說明
    找不到的原因(可為 None),會放進 502 回應 body,但不進例外訊息。"""

    error_code = "NO_USABLE_RESULTS"
    default_message = "no usable restaurants in upstream response"

    def __init__(self, notes=None, *, raw_detail=None):
        super().__init__(raw_detail=raw_detail)
        self.notes = notes


class RecommendationEngine:
    """所有推薦引擎的共同介面(D7)。"""

    # 建立 pending 紀錄時先寫入的模型名稱(設定值);成功後改為上游回應的實際模型。
    model_name = ""

    def is_available(self) -> bool:
        raise NotImplementedError

    def recommend(self, context: RecommendationContext) -> RecommendationResult:
        raise NotImplementedError
