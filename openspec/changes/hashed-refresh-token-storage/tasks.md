> TDD 排法同前幾個 change：先寫會失敗的測試（RED）→ 只寫剛好讓它通過的最小實作（GREEN），垂直切。這個 change 不引入新工具鏈／新依賴（`hashlib` 是標準函式庫）。這次是純內部儲存機制替換，`.openspec.yaml` 已設 `skip_specs: true`，tasks 不需要對應到 spec 需求名稱。

## 1. `RefreshTokenRecord` model 與移除 `token_blacklist` app（非 seam，不走 TDD cycle）

- [ ] 1.1 在 `apps/accounts/models.py` 新增 `RefreshTokenRecord`（`jti` unique+index、`token_hash` unique+index、`user` FK on_delete=CASCADE、`created_at` auto_now_add、`expires_at`、`revoked_at` nullable），照 design.md 的欄位定義 — (auto) `python manage.py check` 無錯
- [ ] 1.2 `python manage.py migrate token_blacklist zero`，然後在 `config/settings/base.py` 的 `INSTALLED_APPS` 移除 `"rest_framework_simplejwt.token_blacklist"`，移除 `SIMPLE_JWT` 裡的 `"BLACKLIST_AFTER_ROTATION"` 那行 — (auto) `python manage.py check` 無錯，且 `python manage.py showmigrations token_blacklist` 應該報這個 app 不存在／無此 app
- [ ] 1.3 `makemigrations accounts && migrate`，套用 `RefreshTokenRecord` 的新 migration — (auto) migration 指令 exit code 0、無錯誤輸出

## 2. Seam: 撤銷紀錄查詢／登記的共用邏輯

- [ ] 2.1 [RED] 在 `apps/accounts/tests/test_services.py`（或新建 `apps/accounts/tests/test_refresh_tokens.py`，你判斷放哪個檔案更合適）寫測試，針對你要實作的共用函式（例如 `record_refresh_token(user, token) -> RefreshTokenRecord`、`get_valid_refresh_token_record(token) -> RefreshTokenRecord | None`、`revoke_refresh_token_record(record)`，實際命名你自己定，只要語意清楚）：① 登記一筆後，用同一個 token 字串能查回同一筆紀錄 ② 查詢時傳的是雜湊過的值存進 DB，不是明文（直接查 DB row 斷言 `token_hash` 不等於原始 token 字串、且不包含原始字串當子字串）③ 已撤銷的紀錄（`revoked_at` 有值）查詢應視為無效 ④ 已過期的紀錄（`expires_at` 是過去時間）查詢應視為無效 ⑤ 查無紀錄回傳無效。確認先是 FAIL（這些函式還不存在）— (auto) `pytest` 顯示 FAIL
- [ ] 2.2 [GREEN] 實作這組函式，用 `hashlib.sha256(token.encode()).hexdigest()` 算 hash，不對外暴露原始 token 字串（函式簽名可以吃到原始 token 字串當參數，但寫進 DB 前一定要先雜湊）。讓 2.1 轉綠 — (auto) `pytest` 該檔全綠

## 3. Seam: `GoogleLoginView`／`RefreshView`／`LogoutView` 改用新的撤銷紀錄機制

- [ ] 3.1 [RED] 修改 `apps/accounts/tests/test_views.py` 既有的登入／refresh／logout 相關測試（不用整批重寫，先跑一次確認哪些會因為底層機制換掉而壞掉，針對性修正），額外新增：① 登入成功後，資料庫的 `RefreshTokenRecord` 表多一筆，且這筆的 `token_hash` 不等於 response cookie 裡的明文 refresh token 字串（直接反查 DB 斷言看不到明文）② `RefreshView` 換發成功後，舊的 `RefreshTokenRecord` 被標記 `revoked_at`、新增一筆新的紀錄 ③ 用已經被撤銷的 refresh cookie 打 `/api/auth/refresh/` → 401（不管是登出撤銷的還是 rotate 後舊的都要測到）④ 查無對應紀錄（例如自己捏造一個結構正確但沒登記過的 JWT 字串）打 `/api/auth/refresh/` → 401 ⑤ `LogoutView` 撤銷時走新表查 `record.user_id` 判斷歸屬（既有的跨使用者測試應該繼續通過，但要確認走的是新邏輯不是舊的 JWT payload 解析）。確認這組測試現在對應到還沒改的 view 邏輯會 FAIL 或行為不符預期 — (auto) `pytest` 顯示 FAIL
- [ ] 3.2 [GREEN] 改寫 `GoogleLoginView`（核發時呼叫 `record_refresh_token`）、`RefreshView`（依 design.md「驗證流程順序」：先查表 → 表通過才做 `RefreshToken(value)` JWT 驗證 → 通過後 rotate＋撤銷舊紀錄＋登記新紀錄）、`LogoutView`（查表拿 `record.user_id` 比對，撤銷用 `revoke_refresh_token_record`，不再呼叫 `token.blacklist()`／`token.outstand()`，這兩個方法在 app 移除後也已經不存在了）。讓 3.1 全部轉綠 — (auto) `pytest` 該檔全綠

## 4. 收尾

- [ ] 4.1 跑 `uv run pytest`（整個專案）、`uv run ruff check .`、`uv run python manage.py check`，三者皆需乾淨 — (auto) 三個指令 exit code 皆 0
- [ ] 4.2 確認 Postgres 裡真的沒有明文 token：`docker exec` 進資料庫用 `psql` 查 `accounts_refreshtokenrecord` 表的 `token_hash` 欄位內容，人工目視確認長度是 64 個十六進位字元、看不出任何 JWT 結構（JWT 明文會有兩個 `.` 分隔符跟看得出 base64url 的三段結構，hash 不會） — (manual) 你自己跑指令看一眼結果
- [ ] 4.3 確認邊界：`git diff --stat develop...HEAD -- apps/events apps/recommendations apps/notifications` 應無輸出 — (auto) 指令輸出為空
