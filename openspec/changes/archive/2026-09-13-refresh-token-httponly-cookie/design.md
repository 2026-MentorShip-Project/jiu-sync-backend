## Context

`user-auth` capability 已經 archive 進主 spec（`openspec/specs/user-auth/spec.md`），目前的實作（`apps/accounts/views.py`）是純 body-based JWT：`GoogleLoginView` 回應 body 帶 `{access, refresh, user}`，`refresh/` 直接掛 simplejwt 內建的 `TokenRefreshView`（從 body 讀 `refresh`），`LogoutView` 也是從 body 讀 `refresh` 撤銷。這次只改「`refresh` 怎麼傳遞」，`access` 維持現狀（body 回傳、前端存 `localStorage`／記憶體、手動帶 header）。見 proposal.md - Why。

## Goals / Non-Goals

**Goals:**
- `refresh` 完全不進入前端 JS 摸得到的地方（不在 response body、不在 `localStorage`），XSS 曝險上限壓回 `access` 的 2 小時
- `access` 的既有機制（header-based、無狀態驗證）完全不變，不因為這次改動牽連

**Non-Goals:**
- 不做完整的 CSRF token 機制——`/api/auth/refresh/`／`/api/auth/logout/` 是這次唯一會讀 cookie 的端點，攻擊者就算誘導受害者瀏覽器打這兩支，回應內容（新 `access`／撤銷結果）攻擊者的網站讀不到（CORS 擋跨站讀 response body），沒有實際可利用的攻擊路徑，不值得為此加一整套 CSRF token 交換機制
- 不處理「已經在使用中、refresh 存在 localStorage 的既有前端流程」的相容/遷移——這個產品還沒有真實使用者，沒有既有 session 要相容

## Decisions

**Cookie 名稱：`refresh_token`（跟 JSON body 過去用的 `refresh` 這個字區分開，避免文件/程式碼裡討論時搞混「JSON 欄位」跟「cookie 名稱」）。**

**Cookie 屬性：`HttpOnly`、`Secure`、`SameSite=None`、`Path=/api/auth/`。**
- `HttpOnly`：JS 讀不到，這是這次改動的核心目的
- `Secure`＋`SameSite=None`：前後端是不同網域的跨站請求，沒有 `SameSite=None` 瀏覽器不會在跨站 fetch 帶這個 cookie；`SameSite=None` 依瀏覽器規範強制要求搭配 `Secure`（只能在 HTTPS 下設定/傳遞），dev/prod 都用同一組屬性、不因環境分支——**本機開發環境要能收到這個 cookie，前端 dev server 也要跑 HTTPS**（已跟使用者確認：用 Vite proxy + 本機 HTTPS〔mkcert 或等效工具〕，前端負責設定，後端不需要為此開分支邏輯）
- `Path=/api/auth/`：把這個 cookie 限制在只有這幾支登入相關端點會自動帶上，不會在 `/api/events/...` 之類的一般 API 請求裡被夾帶，縮小曝露範圍、減少每個請求的 payload
- `Max-Age`：對齊 `SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"]`（14 天），讓 cookie 存活期跟 token 本身的有效期一致，不會有「cookie 沒了但 token 理論上還沒過期」或反過來的落差

**`refresh/` 端點不能再用 simplejwt 內建的 `TokenRefreshView`，要自己包一層。**
內建版只認 request body 的 `refresh` 欄位，改成讀 cookie 需要自訂 view：從 `request.COOKIES` 拿 `refresh`，其餘驗證/換發邏輯沿用 simplejwt 的 `RefreshToken` 類別（跟現有 `LogoutView` 已經在用的模式一致）；換發成功後，因為 `ROTATE_REFRESH_TOKENS=True`，要在回應裡重新 `Set-Cookie` 新的 `refresh`，不能只回傳新 `access` 就結束。

**`GoogleLoginView` 回應不再包含 `refresh` 欄位。**
Body 只回 `{access, user}`，`refresh` 完全走 `Set-Cookie`。這是刻意的收斂——如果 body 還留著 `refresh` 欄位，等於前端還是拿得到、還是可能被存進 `localStorage`，整個修正就沒意義。

**`LogoutView` 改讀 cookie，撤銷後主動清掉 cookie。**
不只撤銷 blacklist，還要在回應裡對同一個 `Path`／名稱下發 `Max-Age=0` 的 `Set-Cookie` 讓瀏覽器立刻刪除這個 cookie，避免瀏覽器端留著一個已經被伺服器端撤銷、但客戶端還看得到「存在」的 cookie。

**跨使用者撤銷檢查（`REFRESH_TOKEN_NOT_YOURS`）維持不動。**
Cookie 讓「附帶任意字串當 refresh」這件事變困難很多（瀏覽器只會自動帶自己收到的那個 cookie），但不是不可能（例如同一台裝置被惡意軟體/實體存取竄改），維持這個檢查是低成本的防禦縱深，沒有理由拿掉。

**CORS：`CORS_ALLOW_CREDENTIALS = True`。**
骨架階段（JWT 全部走 header）刻意拿掉這個設定；這次因為要讓瀏覽器願意收送 cookie，重新打開。`CORS_ALLOWED_ORIGINS` 維持明確列出網域（不可為 `*`——Django 的 CORS middleware 本身就禁止 `*` 搭配 `credentials=True`，這不是新的限制，是既有機制的自然結果）。

## Risks / Trade-offs

- **[本機開發需要 HTTPS 才能測]** → 已跟使用者確認的取捨：換取 dev/prod 設定完全一致（不用為 cookie 屬性寫環境分支邏輯），代價是前端本機開發環境要多一道 HTTPS 設定（Vite 插件即可，一次性成本）。
- **[Concurrent refresh race：多分頁同時觸發 refresh，其中一個可能撞上已經被另一個 rotate 掉的 token]** → 這個風險在 rotate 機制本身就存在（`google-sso-login` 那次就已經接受），不是這次新增的問題，cookie 化沒有讓它變得更嚴重（cookie 是瀏覽器層級共享，不是分頁各自獨立存一份，實際上比 localStorage 分頁各自讀寫的情境風險更低一點）。
- **[Cookie 曝露範圍雖然縮到 `/api/auth/`，仍然是「整個瀏覽器」層級，不是分頁層級]** → 沒有比 localStorage 更差，跟現況持平，不是這次修正要解決的問題。
