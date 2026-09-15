from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed


class OptionalJWTAuthentication(JWTAuthentication):
    """``JWTAuthentication`` 的寬鬆版本 —— 僅供 ``EventDetailView`` 使用。

    ``GET /api/events/{id}`` 依 spec 必須「任何人(含未登入)可查詢」,但全域
    ``REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"]`` 設的是嚴格版
    ``JWTAuthentication``:``AllowAny`` 只跳過權限檢查,不會跳過認證本身,帶了
    格式錯誤/簽章不符/過期的 ``Authorization: Bearer`` header 時,``authenticate()``
    仍會拋 ``rest_framework_simplejwt.exceptions.AuthenticationFailed``(``InvalidToken``
    是其子類,涵蓋壞格式/過期兩種常見情況),被 DRF 轉成 401——結果變成「帶壞掉的
    token」比「完全不帶 token」更嚴格,不符合這支端點應有的公開性。

    這裡攔截該例外、回傳 ``None``,讓 DRF 視為匿名請求(等同沒帶 header),而不是
    讓整個 request 401。只套用在 ``EventDetailView``,其餘需要真實登入的 view
    (``EventCreateView``/``EventListView``)仍用全域設定的嚴格
    ``JWTAuthentication``,壞 token 在那兩支應該繼續 401。見 Codex review 修正項目 A。
    """

    def authenticate(self, request):
        try:
            return super().authenticate(request)
        except AuthenticationFailed:
            return None
