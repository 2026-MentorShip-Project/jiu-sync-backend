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

- [x] 6.1 掛上 simplejwt 內建 `TokenRefreshView` 到 `urls.py` 的 `refresh/`；整合驗證：核發的 refresh token 換到新 access token；已被 5.1/5.2 撤銷（blacklist）的 refresh token 再打 `/refresh/` 會被拒絕——滿足需求「Session 刷新」 — (auto) `pytest` 整合測試全綠

## 7. 收尾

- [x] 7.1 跑 `uv run ruff check .` 跟 `uv run python manage.py check`，確認兩者都乾淨無誤；跑全部測試（`uv run pytest`）確認整組綠燈 — (auto) 三個指令 exit code 皆 0
- [x] 7.2 驗證邊界需求「Google SSO 是主揪唯一登入方式」與「主揪身份驗證不影響參與者存取」：檢視這次 change 的異動檔案清單，確認沒有新增任何非 Google 的登入端點，也沒有修改 `apps/events` 或任何參與者相關路由 — (auto) `git diff --stat develop...HEAD` 只顯示 `apps/accounts/`、`config/settings/base.py`、`openspec/` 底下的檔案

## 8. Post-review 修正：主揪帳號沒有可用密碼（對應需求「主揪帳號沒有可用密碼」）

- [x] 8.1 [RED] 滿足需求「主揪帳號沒有可用密碼」：在 `apps/accounts/tests/test_views.py` 對 `test_valid_id_token_returns_200_with_tokens_and_creates_user` 補上斷言：登入建立的 `User`，`has_usable_password()` 為 `False`。先確認這條斷言現在是 FAIL（目前實作用 `User.objects.create()`，會是 `True`）— (auto) `pytest` 顯示 FAIL
- [x] 8.2 [GREEN] 把 `GoogleLoginView` 建立新使用者那段從 `User.objects.create(...)` 改成 `User.objects.create_user(email=..., google_sub=..., display_name=..., avatar_url=...)`，讓 8.1 轉綠 — (auto) `pytest` 該檔全綠

## 9. Post-review 修正：email 唯一性衝突拒絕登入（對應需求「不同 Google 帳號的 email 衝突時拒絕建立」）

- [x] 9.1 [RED] 滿足需求「不同 Google 帳號的 email 衝突時拒絕建立」：在 `apps/accounts/tests/test_views.py` 新增測試：先用 `User.objects.create_user` 建一筆既有使用者（`google_sub="existing-sub"`, `email="shared@example.com"`），再 mock `verify_google_id_token` 回傳一組**不同** `sub`（例如 `"new-sub"`）但**相同** `email`（`"shared@example.com"`）的 claims，打 `POST /api/auth/google/` → 驗證：① 回應狀態碼 409 ② body `code == "EMAIL_ALREADY_IN_USE"` ③ 沒有新增第二筆 `User`（`User.objects.count()` 不變）④ 既有那筆使用者的資料沒有被覆寫。確認先是 FAIL（目前的實作對任何 `IntegrityError` 都無條件假設是 `google_sub` 衝突，這個情境會變成未處理的 `User.DoesNotExist` → 500，不是 409）— (auto) `pytest` 顯示 FAIL
- [x] 9.2 [GREEN] 在 `GoogleLoginView` 捕捉到 `IntegrityError` 後，用 `getattr(exc.__cause__, "diag", None)` 拿 `constraint_name` 判斷實際撞到哪個 constraint：是 `google_sub` 的 unique constraint（並發情境，原本設計要處理的路徑）才走 `User.objects.get(google_sub=...)`；其他情況（尤其 `email` 的 unique constraint）改拋 `ApiError(message, code="EMAIL_ALREADY_IN_USE", status_code=409)`。**注意：既有的併發測試 `test_concurrent_create_integrity_error_falls_back_to_get` mock 的是 `User.objects.create` 直接拋 `IntegrityError("duplicate key value violates unique constraint")`（純字串，沒有真正的 `__cause__.diag`），這個測試要跟著調整成用一個帶正確 `diag.constraint_name`（模擬 `accounts_user_google_sub_key`）的假例外物件，否則新邏輯會把它誤判成不認得的 constraint、原本應該 200 的併發測試會變成意外的 409。** 讓 9.1 轉綠，同時確認既有的併發測試（調整後）跟其他所有測試依然全綠 — (auto) `pytest` 全部檔案全綠

## 10. Post-review 修正：Google claims 必要欄位與 email_verified 驗證（對應需求「缺少必要身份欄位的權杖被拒絕」）

- [x] 10.1 [RED] 滿足需求「缺少必要身份欄位的權杖被拒絕」：在 `apps/accounts/tests/test_services.py` 新增測試：mock `verify_oauth2_token` 分別回傳①缺少 `sub`、②缺少 `email`、③ `email_verified` 為 `False`（或缺少這個欄位）這三種 claims，驗證 `verify_google_id_token` 每一種都拋 `GoogleTokenError`。確認先是 FAIL（目前的實作只驗證 audience/issuer，不檢查這些欄位，這三種情況現在會正常回傳 claims，不會拋例外）— (auto) `pytest` 顯示 FAIL
- [x] 10.2 [GREEN] 在 `verify_google_id_token` 通過 audience/issuer 檢查後，追加驗證：`claims.get("sub")` 非空、`claims.get("email")` 非空且用 `django.core.validators.validate_email` 通過格式檢查、`claims.get("email_verified") is True`，任一不符合就拋 `GoogleTokenError`（不額外分 code，沿用同一種 401）。讓 10.1 轉綠 — (auto) `pytest` 該檔全綠

## 11. Post-review 修正：對外錯誤訊息不得洩漏底層細節（對應需求「對外身份驗證錯誤不洩漏底層細節」）

- [x] 11.1 [RED] 滿足需求「對外身份驗證錯誤不洩漏底層細節」：在 `apps/accounts/tests/test_views.py` 的 `test_invalid_id_token_returns_401_and_creates_no_user` 補上斷言：`response.json()["message"]` 是固定的通用文字（例如檢查它**不等於** mock 設定的原始例外訊息字串，而是等於程式碼裡寫死的那句話），確認先是 FAIL（目前 `message` 就是 `str(exc)` 原始例外文字）— (auto) `pytest` 顯示 FAIL
- [x] 11.2 [GREEN] `GoogleLoginView` 捕捉 `GoogleTokenError` 時，改成 `logging.getLogger(__name__).warning("Google id_token verification failed: %s", exc)` 記錄原始例外內容（不記錄 token 本身），對外一律 `raise ApiError("Google 登入驗證失敗，請重新登入", code="INVALID_ID_TOKEN", status_code=401)` 固定文字。讓 11.1 轉綠 — (auto) `pytest` 該檔全綠

## 12. Post-review 修正：登出前確認 refresh token 屬於呼叫者本人（對應需求「不得撤銷他人的刷新憑證」）

- [x] 12.1 [RED] 滿足需求「不得撤銷他人的刷新憑證」：在 `apps/accounts/tests/test_views.py` 新增測試：建立兩個使用者 A、B，A 登入拿到 access token，B 登入拿到 refresh token；用 A 的 access token 當 `Authorization`，打 `POST /api/auth/logout/` 帶 B 的 refresh token → 驗證：① 回應 403，`code == "REFRESH_TOKEN_NOT_YOURS"` ② B 的 refresh token 事後仍然有效（拿去打 `/api/auth/refresh/` 還是能換到新 access，沒有被撤銷）。確認先是 FAIL（目前的實作沒有做這個比對，會直接撤銷成功回 205）— (auto) `pytest` 顯示 FAIL
- [x] 12.2 [GREEN] `LogoutView` 在呼叫 `.blacklist()` 之前，先建構 `RefreshToken(refresh_token)`（此時已經驗證過簽章／格式，複用同一個 `TokenError` 例外處理路徑），讀取 `token["user_id"]`，跟 `str(request.user.id)` 比對，不符就 `raise ApiError(message, code="REFRESH_TOKEN_NOT_YOURS", status_code=403)`，不執行撤銷；相符才繼續原本的 `.blacklist()` 流程。讓 12.1 轉綠 — (auto) `pytest` 該檔全綠

## 13. 收尾（重跑，確認全部 post-review 修正沒有互相破壞）

- [x] 13.1 跑 `uv run pytest`（整個專案）、`uv run ruff check .`、`uv run python manage.py check`，三者皆需乾淨 — (auto) 三個指令 exit code 皆 0
- [x] 13.2 重新確認邊界需求：`git diff --stat develop...HEAD -- apps/events apps/recommendations apps/notifications` 仍應無輸出 — (auto) 指令輸出為空
