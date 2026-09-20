from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed


class OptionalJWTAuthentication(JWTAuthentication):
    """``JWTAuthentication`` 的寬鬆版本 —— 僅供公開端點使用。

    ``AllowAny`` 只跳過權限檢查,不會跳過認證本身:帶了格式錯誤/過期的
    ``Authorization: Bearer`` header 時,``authenticate()`` 仍會拋
    ``AuthenticationFailed``、被 DRF 轉成 401——結果變成「帶壞 token」比「完全
    不帶 token」更嚴格,不符合公開端點的預期。這裡攔截該例外回傳 ``None``,
    讓 DRF 視為匿名請求。
    """

    def authenticate(self, request):
        try:
            return super().authenticate(request)
        except AuthenticationFailed:
            return None
