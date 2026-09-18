## Context

`apps/accounts` 目前是空 Django app 骨架（沒有 model，`serializers.py`/`views.py`/`urls.py` 都只有 import 占位）。專案設定已經裝好 `djangorestframework-simplejwt`（含 `rest_framework_simplejwt.token_blacklist`）、`google-auth`，也已經有 `GOOGLE_OAUTH_CLIENT_ID` 設定但目前沒用到。`AUTH_USER_MODEL` 在骨架階段刻意先不設（當時還沒有任何 model）；這次 change 就是要把它定案。目前沒有任何既有使用者資料——這是這個後端的第一個 model，不涉及資料遷移。動機詳見 proposal.md - Why。

## Goals / Non-Goals

**Goals:**
- 做出一套能跑通的 Google SSO 登入流程（驗證 → 辨識主揪 → 核發 session）
- 把 Google 權杖驗證邏輯跟 view 層隔開，未來要換掉也不必動請求/回應介面
- 沿用 settings 裡已經設好的 JWT / blacklist 基礎設施

**Non-Goals:**
- django-allauth 或任何其他身份提供者整合——先延後；現在的需求就是 Google-only，上面提到的隔離設計是留了門，不是現在就先做出一層抽象
- 任何參與者（被揪者）相關行為——proposal.md 已明確排除
- 登入端點的 rate limiting / 暴力破解防護——權杖驗證整個委託給 Google，沒有密碼可以暴力破解，等真的出現濫用模式再處理

## Decisions

**手動驗證 id_token，不用 django-allauth。**
前端自己走完 Google OAuth 流程，把拿到的 Google `id_token`交給後端。後端唯一要做的是驗證這個權杖（`google.oauth2.id_token.verify_oauth2_token`，檢查 audience 是否對應 `GOOGLE_OAUTH_CLIENT_ID`、檢查 issuer），然後核發自己的 session。考慮過的替代方案是 django-allauth，它會把整個 OAuth 流程都攬在後端。目前先不用，因為這個產品只需要一個 provider，allauth 的 session/adapter 機制比 PRD 那個「一鍵登入」的流程需要的複雜度高出不少。這段驗證邏輯獨立寫在 `apps/accounts/services.py`，不塞進 view 裡——這就是未來要換 allauth 或加第二個 provider 時該替換的那一層，不需要動到 `views.py` 的請求/回應介面。

**自訂 `User` model，`email` 當 `USERNAME_FIELD`，`google_sub` 當穩定身份鍵。**
`AbstractBaseUser` + `PermissionsMixin`，沒有可用密碼（建立時透過自訂 `UserManager` 呼叫 `set_unusable_password()`）。get-or-create 用 `google_sub`（Google 的 `sub` claim）當 key，因為 email 在 Google 端理論上可能變動，但 `sub` 不會；`email` 仍設為 `USERNAME_FIELD`，因為 Django admin 跟人工查找都預期用這個欄位。主鍵用 UUID（不是預設的自增整數），這樣萬一主揪識別碼未來被暴露在 URL 或回應裡，也不容易被枚舉。

**Django admin superuser 仍保留密碼登入。**
`UserManager.create_superuser` 接受真的密碼。這是「Google SSO only」規則刻意留的一個小例外——它管的是內部維運用的 `/admin/` 存取，不是 PRD §2.1 規範的主揪產品端 API。`createsuperuser` 指令維持可用，供 Django admin 初始化帳號。

**每次登入都同步更新 `email`／`display_name`／`avatar_url`。**
不是只在首次建立 `User` 時寫入一次；`google_sub` 對應的既有使用者再次登入時，這三個欄位一律用當次 claims 覆寫。理由：這些欄位的正確來源是 Google，本地資料只是快取，若不同步，使用者在 Google 端改頭像/顯示名稱後本地會永久跟實際狀態脫節，而目前沒有任何介面讓主揪自行修正這份快取。

**同帳號並發登入的 get-or-create 用 `transaction.atomic()` + 捕捉 `IntegrityError` 後改 `get()`。**
`google_sub` 有 unique 約束；兩個幾乎同時的登入請求都通過 Google 驗證、幾乎同時嘗試建立同一個 `google_sub` 的 `User` 時，其中一個 insert 會撞上 unique constraint 拋出 `IntegrityError`。處理方式：包在 `transaction.atomic()` 裡先嘗試 `create()`，捕捉 `IntegrityError` 後改用 `get(google_sub=...)` 拿到另一個請求剛建好的那筆，不讓任一邊的登入請求整個失敗。考慮過「不特別處理、接受極小機率下兩邊都失敗要重試」，但這個 race window 雖窄，一旦命中會讓使用者體驗上像是登入偶發性壞掉且原因不明，值得用幾行程式碼換掉這個不確定性。

**JWT 效期與 rotation** 已經在 `config/settings/base.py` 定案（`SIMPLE_JWT`：access 2 小時／refresh 14 天，refresh 時 rotate、rotate 後舊 token 進 blacklist）——這次 change 是照這個設定實作，不是重新決定數值。

**端點形狀**，都掛在 `/api/auth/` 下（`config/urls.py` → `apps/accounts/urls.py` 已經接好）：
- `POST /api/auth/google/` — body `{idToken}`（欄位命名跟前端既有 Swagger 文件對齊，非 Python 慣用的 snake_case），回傳 `{access, refresh, user}`（或 401）
- `POST /api/auth/refresh/` — 用 simplejwt 內建的 `TokenRefreshView`，不用自己寫
- `POST /api/auth/logout/` — body `{refresh}`，把它加進 blacklist
- `GET /api/me/` — `IsAuthenticated`，回傳目前主揪的個人資料

## Post-review 補充決策（2026-09-13）

實作完成後的 code review 抓到幾個真的問題，逐項確認並修正，不是重新設計：

**修正 1：get-or-create 必須真的走 `UserManager.create_user()`，不能用 `User.objects.create()`。**
原本的實作直接呼叫 `User.objects.create(google_sub=..., email=..., ...)`——這是 Django 預設 manager 的 `.create()`，跟自訂的 `UserManager.create_user()` 是兩個不同方法，`.create()` 完全不會呼叫 `set_unusable_password()`。實測確認：這樣建出來的使用者 `password == ""`、`has_usable_password() == True`，直接違反本文件前面「沒有可用密碼」這個決策。改成呼叫 `User.objects.create_user(email=..., google_sub=..., display_name=..., avatar_url=...)`。

**修正 2：`IntegrityError` 要分辨是撞到哪個 unique constraint，不能無條件假設是 `google_sub`。**
`email` 欄位也是 `unique=True`。情境：既有使用者 A（`google_sub=A`, `email=x@example.com`），另一個 Google 帳號 B 登入、Google 回傳的 email 剛好也是 `x@example.com`（`google_sub=B`）——`create_user()` 會因為 `email` unique constraint 失敗，但原本的 `except IntegrityError` 無條件執行 `get(google_sub=B)`，查不到（B 從未成功建立）→ 未處理的 `User.DoesNotExist` → 500。

處理方式：捕捉到 `IntegrityError` 後，用 `exc.__cause__.diag.constraint_name`（psycopg 提供，Postgres-only 這個專案適用）判斷實際撞到哪個 constraint：
- 是 `google_sub` 的 unique constraint → 這才是原本設計要處理的並發情境，走 `get(google_sub=...)`
- 是 `email` 的 unique constraint（或任何其他非預期的 constraint）→ **拒絕登入**，拋 `ApiError(message, code="EMAIL_ALREADY_IN_USE", status_code=409)`。不自動合併帳號、不靜默覆蓋——已跟使用者確認這個政策：不同 Google 帳號共用 email 是異常情況，MVP 階段直接擋下最安全，比自動合併風險低。

**修正 3：Google claims 要驗證必要欄位存在、且 `email_verified` 為真，不能直接信任簽章驗證通過就等於欄位齊全。**
`verify_google_id_token` 目前只驗證簽章（audience/issuer），驗證通過後 view 直接 `claims["sub"]`／`claims["email"]`——如果 claims 缺這些欄位會 `KeyError` → 未格式化的 500。改為在 `services.py` 裡驗證完簽章後，額外檢查：
- `sub` 存在且非空字串
- `email` 存在、非空、格式合法（`django.core.validators.validate_email`）
- `email_verified` 為 `True`（已跟使用者確認：要求這項，但不限制 Workspace 網域——`hd` claim 不驗證，這是對外公開登入的產品，不該限定特定公司網域）

任何一項不符合都視同驗證失敗，拋 `GoogleTokenError`（沿用原本的 401／`INVALID_ID_TOKEN`，不需要為這幾種情況另外開新的 code——對前端來說都是「這個 Google 登入不被接受」，原因對使用者來說不需要細分）。

**已跟使用者確認、這次不處理**：`is_active=False` 的使用者登入時目前沒有攔截檢查——這個專案目前完全沒有任何介面會把 `is_active` 設成 `False`（沒有停權功能），所以現在補這個檢查測不出真正的行為、只是防禦性程式碼。留到真的有停權功能的 change 一起做，不在這裡預先猜測停權後應該回什麼。

**修正 4：Google 驗證失敗的錯誤訊息不能把第三方套件的原始例外文字直接回給前端。**
原本 `GoogleLoginView` 直接 `raise ApiError(str(exc), code="INVALID_ID_TOKEN", status_code=401)`，`str(exc)` 是 `google-auth` 套件或本文件修正 3 新增的驗證邏輯產生的原始訊息，會讓對外 API 的措辭綁死第三方套件版本、也可能透露不必要的驗證細節。改為：對外 `message` 固定用一句通用文字（例如「Google 登入驗證失敗，請重新登入」），原始例外內容改用 `logging.getLogger(__name__).warning(...)` 記在伺服器端（不記錄 token 本身，只記錄例外訊息），`code=INVALID_ID_TOKEN` 維持不變，前端本來就該依賴 `code`、不是解析 `message` 文字。

**修正 5：登出前要確認 refresh token 屬於呼叫者本人。**
原本 `LogoutView` 只要求呼叫者帶合法 access token，之後直接撤銷 request body 裡指定的 refresh token，沒比對這個 refresh token 到底是不是呼叫者自己的——持有任意合法 access token的人可以撤銷別人的 refresh token（前提是要先拿到那個 token，不是帳號接管，但仍是不該允許的跨使用者操作，也不符合 spec「撤銷『其』刷新憑證」的用詞）。修正：`LogoutView` 在真的呼叫 `.blacklist()` 之前，先解析這個 refresh token 拿出它的 `user_id`（`SIMPLE_JWT["USER_ID_CLAIM"]` 設的是 `"user_id"`），跟 `request.user.id` 比對，不符就拋 `ApiError(message, code="REFRESH_TOKEN_NOT_YOURS", status_code=403)`，不執行撤銷。

## Risks / Trade-offs

- **[Google 前端函式庫或流程改版]** → 後端只信任拿到的 `id_token`，前端的 OAuth 流程怎麼變都好，後端這個「傳 id_token 換 JWT」的介面不用跟著變，影響範圍有限。
- **[Blacklist 資料表持續變大]** → simplejwt 的 blacklist app 每 rotate 一次或每次登出都會多一筆已撤銷 token 紀錄，長期會累積。這次先不處理，記一筆待辦：之後可以用現有的 Celery 排一個定期清理任務（例如跑 `flushexpiredtokens`），不在這次範圍內解決。
- **[手動驗證邏輯久了跟不上 Google 官方建議]** → 靠使用官方 `google-auth` 套件的 `verify_oauth2_token`（處理 Google 公鑰輪替、標準檢查如過期/issuer）降低風險；`services.py` 的隔離設計也讓未來要修只需要改一個檔案。
