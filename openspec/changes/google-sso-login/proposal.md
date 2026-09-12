## Why

PRD（登入權限與路由控制 & AI 聚餐選餐廳 產品修改規格書 §2.1/2.4）規定主站管理頁強制 Google SSO 登入，且只保留這一個登入管道（明確排除自建帳密、簡訊 OTP、安全提示問題）。目前 `apps/accounts` 只有空殼檔案，後端完全沒有「誰是主揪」的機制，之後所有需要主揪身份的功能（建立活動、我的聚會列表、活動管理）都會卡在這個依賴上，必須先落地。

## What Changes

- 新增 `accounts.User` model：以 email 為 `USERNAME_FIELD`，`google_sub`（Google 帳號 subject id）唯一值，無可用密碼（Google SSO only，`set_unusable_password()`）
- 後端驗證前端傳來的 Google `id_token`：用 `google-auth` 套件驗證 audience（比對 `GOOGLE_OAUTH_CLIENT_ID`）與 issuer，驗證失敗回 401
- 登入 API：`POST /api/auth/google/`，帶 `idToken`（跟前端既有的 Swagger 文件命名對齊），驗證通過後 get-or-create User，換發本站 JWT（access/refresh，simplejwt）
- Refresh API：`POST /api/auth/refresh/`（simplejwt 內建 `TokenRefreshView`）
- 登出 API：`POST /api/auth/logout/`，撤銷 refresh token（用已裝好的 `rest_framework_simplejwt.token_blacklist`）
- 個人資料 API：`GET /api/auth/me/`，回傳目前登入主揪的 email/display_name/avatar_url，需帶 access token
- 不包含：django-allauth 整合、Google 以外的登入方式、任何參與者（被揪者）相關端點——PRD §2.2 參與者頁維持完全免登入，此次變更不觸碰

## Capabilities

### New Capabilities
- `user-auth`：主揪 Google SSO 登入、本站 JWT 簽發/刷新/撤銷、取得自身身份資料

### Modified Capabilities

（無——這是本後端第一個落地的 capability，尚無既有 spec 可修改）

## Impact

- `apps/accounts/models.py`：新增 `User` model 與自訂 `UserManager`
- `apps/accounts/services.py`（新檔）：Google id_token 驗證邏輯，獨立於 view 之外，未來要換成 allauth 或加其他 provider 時只需替換這層
- `apps/accounts/serializers.py`：`GoogleLoginSerializer`、`UserSerializer`
- `apps/accounts/views.py`：`GoogleLoginView`、`LogoutView`、`MeView`
- `apps/accounts/urls.py`：掛上述端點 + simplejwt 的 `TokenRefreshView`
- `config/settings/base.py`：補回 `AUTH_USER_MODEL = "accounts.User"`（骨架階段為求乾淨先拿掉，此次是第一個實際 model 進來的時機）
- `apps/accounts/migrations/`：新增初始 migration
- 不影響 `apps/events`、`apps/recommendations`、`apps/notifications`——仍維持空殼骨架
- 無新增依賴，`google-auth`、`djangorestframework-simplejwt`、`rest_framework_simplejwt.token_blacklist` 骨架階段已裝好/掛好
