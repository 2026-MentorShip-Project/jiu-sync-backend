## Why

目前 `refresh` token 跟 `access` 一樣交給前端存在 `localStorage`。這代表萬一網站出現 XSS 漏洞，攻擊者一次可以偷走兩者——`access` 只活 2 小時影響有限，但 `refresh` 活 14 天，等於曝險上限是 14 天而不是 2 小時。把 `refresh` 改成後端用 `httpOnly` cookie 下發，JS（含惡意注入的 XSS script）完全讀不到這個值，能把曝險上限壓回 `access` 的 2 小時。`access` 維持現狀（前端存 `localStorage`／記憶體，手動帶 `Authorization` header）——不改整套成 session 架構，只動 `refresh` 這一個環節，維持 JWT 無狀態、跨網域部署友善的既有決策不變。

## What Changes

- `POST /api/auth/google/`：response body 不再回傳 `refresh` 欄位（前端 JS 本來就不該摸到它），改用 `Set-Cookie` 下發（`HttpOnly`、`Secure`、`SameSite=None`、`Path=/api/auth/`，效期對齊 `SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"]`）
- `POST /api/auth/refresh/`：改從 cookie 讀 `refresh`（不再接受 body 帶的 `refresh`），換發新 `access` 的同時（`ROTATE_REFRESH_TOKENS=True`）也要重新 `Set-Cookie` 新的 `refresh`
- `POST /api/auth/logout/`：改從 cookie 讀 `refresh` 撤銷，撤銷後同時清掉這個 cookie（`Max-Age=0`）
- `CORS_ALLOW_CREDENTIALS` 改回 `True`（骨架階段拿掉過，這次因為要讓瀏覽器願意收送這個 cookie 而重新需要）；`CORS_ALLOWED_ORIGINS` 維持明確列出（不可為 `*`，Django 本來就禁止 `*` 搭配 credentials）
- 登出的跨使用者 refresh 撤銷檢查（`REFRESH_TOKEN_NOT_YOURS`）維持不動——雖然 cookie 讓「任意夾帶別人 token」變困難很多，但這是防禦縱深，成本低不需要拿掉

## Capabilities

### New Capabilities

（無）

### Modified Capabilities
- `user-auth`：`主揪 Session 核發`、`Session 刷新`、`登出撤銷 Session` 這三個既有需求的「refresh 憑證怎麼傳遞」這件事改變（body → httpOnly cookie），行為意圖不變（換發、撤銷邏輯不變），只是傳輸機制不同

## Impact

- `apps/accounts/views.py`：`GoogleLoginView`（拿掉 response body 的 `refresh`，改 `Set-Cookie`）、新增自訂 `RefreshView`（取代原本直接掛 simplejwt 內建 `TokenRefreshView`，因為內建版只認 body，要包一層改讀 cookie）、`LogoutView`（改讀 cookie、登出後清 cookie）
- `apps/accounts/urls.py`：`refresh/` 從內建 `TokenRefreshView` 換成自訂 view
- `config/settings/base.py`：`CORS_ALLOW_CREDENTIALS = True`
- 前端需要調整：`/api/auth/google/`、`/api/auth/refresh/`、`/api/auth/logout/` 這三支呼叫要帶 `credentials: 'include'`；其他 API 呼叫不受影響；response body 不會再拿到 `refresh` 欄位
- 不影響 `apps/events`、`apps/recommendations`、`apps/notifications`
