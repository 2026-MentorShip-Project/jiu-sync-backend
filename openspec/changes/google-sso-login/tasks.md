> TDD 排法：每個 seam 都是「先寫會失敗的測試（RED）→ 只寫剛好讓它通過的最小實作（GREEN）」一組，垂直切，不要把所有測試一次寫完才開始實作。Seam 範圍已與使用者確認：`verify_google_id_token` 單獨測；`/api/auth/google/`、`/api/auth/logout/`、`/api/me/` 三個走 HTTP 層測；`/api/auth/refresh/` 是 simplejwt 內建 view，不開獨立 cycle，只做整合驗證。
>
> 這個 change 不引入新工具鏈／新依賴（`google-auth`、`djangorestframework-simplejwt`、`token_blacklist` 骨架階段已裝好），依規則不需要獨立的環境健檢 task。
>
> 每項驗證方式前綴 `(auto)`／`(manual)`：這裡全部是後端 API/model，全部可由 `pytest`／`ruff`／`manage.py` 指令自動驗證，沒有需要人工操作確認的步驟。

## 1. 前置：Settings 與 User Model（非 seam，不走 TDD cycle）

- [x] 1.1 在 `config/settings/base.py` 設定 `AUTH_USER_MODEL = "accounts.User"` — (auto) `python manage.py check`（此刻預期報錯，因為 model 還沒建，1.1/1.2 同一個 commit 完成才會轉正常）
- [x] 1.2 實作 `apps/accounts/models.py`：`User(AbstractBaseUser, PermissionsMixin)`，含 UUID `id`、唯一 `email`（`USERNAME_FIELD`）、唯一 `google_sub`、`display_name`、`avatar_url`、`is_active`、`is_staff`、`date_joined`；自訂 `UserManager`，含 `create_user(email, google_sub, **extra)`（無可用密碼）與 `create_superuser(email, password, google_sub="", **extra)` — (auto) `python manage.py check` 轉為無錯
- [x] 1.3 產生並套用初始 migration（`makemigrations accounts && migrate`），對本機 Postgres（docker compose）跑過 — (auto) migration 指令本身 exit code 0、無錯誤輸出
- [x] 1.4 確認 `python manage.py createsuperuser --noinput`（搭配 `DJANGO_SUPERUSER_EMAIL`/`DJANGO_SUPERUSER_PASSWORD`/`DJANGO_SUPERUSER_GOOGLE_SUB=""` 環境變數）可正常建立一個可用密碼登入 `/admin/` 的帳號（design.md 的 superuser 例外）— (auto) 指令 exit code 0 且資料庫多一筆 `is_superuser=True` 的 `User`

## 2. Seam: `verify_google_id_token`（對應 spec 需求：Google 身份驗證）

- [x] 2.1 [RED] 在 `apps/accounts/tests/test_services.py` 寫測試（mock `google.oauth2.id_token.verify_oauth2_token`）：① 合法 claims（audience 對、issuer 對、未過期）回傳 claims；② audience 不符 → 拋 `GoogleTokenError`；③ issuer 不是 `accounts.google.com`/`https://accounts.google.com` → 拋 `GoogleTokenError`；④ 底層函式庫拋 `ValueError`（過期/格式錯）→ 拋 `GoogleTokenError`。確認這組測試現在跑起來是 FAIL（`verify_google_id_token`／`GoogleTokenError` 還不存在）— (auto) `pytest` 顯示這幾條 FAIL
- [x] 2.2 [GREEN] 在 `apps/accounts/services.py` 實作 `GoogleTokenError` 與 `verify_google_id_token(token) -> dict`，只寫到讓 2.1 全部轉綠為止，不多加東西——滿足需求「Google 身份驗證」 — (auto) `pytest` 該檔全綠

## 3. Seam: `POST /api/auth/google/`（對應 spec 需求：主揪 Session 核發、回訪主揪的個人資料隨 Google 端更新同步）

- [x] 3.1 [RED] 在 `apps/accounts/tests/test_views.py` 寫 API 測試（走 DRF test client 打 HTTP，`verify_google_id_token` 用 mock 替換掉，不真的呼叫 Google）：
  ① 合法 `idToken` → 200（跟前端 Swagger 契約對齊，固定 200，不分新建/既有使用者），回傳 body 含 `access`/`refresh`/`user`，且資料庫多一筆 `User`
  ② 同一個 `google_sub` 的合法 `idToken` 再打一次 → 不新增第二筆 `User`（get-or-create）
  ③ `verify_google_id_token` 拋 `GoogleTokenError` → 回 401（body 符合 `api-error-format` change 定義的 `{message, code}` 形狀），不建立任何 `User`
  ④ 已存在的 `google_sub`，claims 的 email／display_name／avatar_url 跟本地紀錄不同時再登入 → 該筆 `User` 對應欄位被更新為本次 claims 的值（design.md「每次登入都同步更新」）
  ⑤ 併發：mock `User.objects.create` 讓第一次呼叫拋 `IntegrityError`（模擬另一個並發請求搶先 insert 成功），驗證 view 改走 `get(google_sub=...)` 拿到既有那筆、正常回應 200 並核發 token，而不是讓請求整個失敗（design.md「並發登入保護」）
  確認這組測試現在是 FAIL（view 還沒接上）— (auto) `pytest` 顯示這幾條 FAIL
- [x] 3.2 [GREEN] 實作 `GoogleLoginSerializer`（`apps/accounts/serializers.py`）、`GoogleLoginView`（`apps/accounts/views.py`，`AllowAny`）：呼叫 `verify_google_id_token`，捕捉 `GoogleTokenError` 時改拋 `config.exceptions.ApiError(message, code="INVALID_ID_TOKEN", status_code=401)`（`api-error-format` change 已完成，直接用，不要自己手刻 401 回應）→ 在 `transaction.atomic()` 內先嘗試 `User.objects.create(google_sub=..., email=..., display_name=..., avatar_url=...)`，捕捉 `IntegrityError` 則改 `User.objects.get(google_sub=...)` 並用當次 claims 更新 `email`/`display_name`/`avatar_url` 後 `save()`（既有使用者的一般路徑也走這個更新，不只並發衝突時才做）→ `RefreshToken.for_user(user)`，掛到 `apps/accounts/urls.py` 的 `google/`，讓 3.1 全部轉綠——滿足需求「主揪 Session 核發」與「回訪主揪的個人資料隨 Google 端更新同步」 — (auto) `pytest` 該檔全綠

## 4. Seam: `GET /api/me/`（對應 spec 需求：已登入主揪的身份查詢）

- [x] 4.1 [RED] 寫 API 測試：① 帶合法 access token → 200，回傳正確的 `id`/`email`/`display_name`/`avatar_url`/`date_joined`；② 不帶 token → 401。確認現在是 FAIL — (auto) `pytest` 顯示 FAIL
- [x] 4.2 [GREEN] 實作 `UserSerializer`（唯讀）、`MeView`（`IsAuthenticated`），掛到 `urls.py` 的 `me/`，讓 4.1 轉綠——滿足需求「已登入主揪的身份查詢」 — (auto) `pytest` 該檔全綠

## 5. Seam: `POST /api/auth/logout/`（對應 spec 需求：登出撤銷 Session）

- [x] 5.1 [RED] 寫 API 測試：① 帶合法 refresh token → 205，且該 token 被加入 blacklist（之後這個 refresh 對 `/api/auth/refresh/` 會被拒絕，可在此測試內直接驗證，或留給 6.1 整合驗證覆蓋）；② 帶無效/格式錯的 refresh → 400。確認現在是 FAIL — (auto) `pytest` 顯示 FAIL
- [x] 5.2 [GREEN] 實作 `LogoutView`（`IsAuthenticated`，`RefreshToken(token).blacklist()`），掛到 `urls.py` 的 `logout/`，讓 5.1 轉綠——滿足需求「登出撤銷 Session」 — (auto) `pytest` 該檔全綠

## 6. `/api/auth/refresh/` 整合驗證（不開獨立 TDD cycle，對應 spec 需求：Session 刷新）

- [ ] 6.1 掛上 simplejwt 內建 `TokenRefreshView` 到 `urls.py` 的 `refresh/`；整合驗證：核發的 refresh token 換到新 access token；已被 5.1/5.2 撤銷（blacklist）的 refresh token 再打 `/refresh/` 會被拒絕——滿足需求「Session 刷新」 — (auto) `pytest` 整合測試全綠

## 7. 收尾

- [ ] 7.1 跑 `uv run ruff check .` 跟 `uv run python manage.py check`，確認兩者都乾淨無誤；跑全部測試（`uv run pytest`）確認整組綠燈 — (auto) 三個指令 exit code 皆 0
- [ ] 7.2 驗證邊界需求「Google SSO 是主揪唯一登入方式」與「主揪身份驗證不影響參與者存取」：檢視這次 change 的異動檔案清單，確認沒有新增任何非 Google 的登入端點，也沒有修改 `apps/events` 或任何參與者相關路由 — (auto) `git diff --stat develop...HEAD` 只顯示 `apps/accounts/`、`config/settings/base.py`、`openspec/` 底下的檔案
