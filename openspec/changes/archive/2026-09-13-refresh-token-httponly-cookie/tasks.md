> TDD 排法同前幾個 change：先寫會失敗的測試（RED）→ 只寫剛好讓它通過的最小實作（GREEN），垂直切。這個 change 不引入新工具鏈／新依賴。DRF test client（`APIClient`）會自動在同一個 client 實例的請求之間保存並附帶 cookie（跟瀏覽器行為一致），可以直接拿來測完整的登入→刷新→登出流程，不用手動組 cookie header。

## 1. Seam: `GoogleLoginView` 改用 `Set-Cookie` 傳遞 refresh（對應需求「刷新憑證對前端程式碼不可讀取」）

- [x] 1.1 [RED] 滿足需求「刷新憑證對前端程式碼不可讀取」：在 `apps/accounts/tests/test_views.py` 修改／新增測試，驗證合法登入的回應：① body **不含** `refresh` 欄位（只有 `access`、`user`）② response 有一個名叫 `refresh_token` 的 cookie，且該 cookie 的 `httponly` 屬性為真 ③ 該 cookie 的值是一個有效、可以拿去換發的 refresh token（可以直接用它打 `RefreshToken(cookie_value)` 不拋例外來驗證，不用真的打 refresh 端點，那是 task 2 的事）。確認先是 FAIL（目前 body 還是帶 `refresh`，也還沒設定任何 cookie）— (auto) `pytest` 顯示 FAIL
- [x] 1.2 [GREEN] 修改 `GoogleLoginView`：response body 拿掉 `refresh` 欄位；改用 `response.set_cookie("refresh_token", str(refresh), max_age=..., httponly=True, secure=True, samesite="None", path="/api/auth/")`，`max_age` 用 `settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds()` 算，不要寫死秒數。讓 1.1 轉綠 — (auto) `pytest` 該檔全綠

## 2. Seam: 自訂 `RefreshView`（對應需求「有效的刷新憑證換得新的存取憑證」「已撤銷或過期的刷新憑證被拒絕」的 cookie 化）

- [x] 2.1 [RED] 滿足需求「Session 刷新」「有效的刷新憑證換得新的存取憑證」：在 `apps/accounts/tests/test_views.py` 新增測試，用同一個 `APIClient` 實例先登入（拿到 `refresh_token` cookie，client 自動記住），直接打 `POST /api/auth/refresh/`（**不在 body 帶任何東西**，完全依賴 client 自動附帶的 cookie）→ 驗證：① 200 ② body 有新的 `access`（非空字串）③ response 有一個新的 `refresh_token` cookie（rotate 後的新值，`httponly` 為真）。再測沒有 cookie 的情況（用一個全新、乾淨的 `APIClient()`，沒登入過）打同一支 → 401。確認先是 FAIL（現在掛的是 simplejwt 內建 `TokenRefreshView`，只認 body，不讀 cookie，這兩種情境都不會是預期結果）— (auto) `pytest` 顯示 FAIL
- [x] 2.2 [GREEN] 在 `apps/accounts/views.py` 新增自訂 `RefreshView(APIView)`：從 `request.COOKIES.get("refresh_token")` 拿值，沒有就拋 `ApiError(..., code="INVALID_REFRESH_TOKEN", status_code=401)`；有值就 `try: token = RefreshToken(value)` 捕捉 `TokenError` 同樣轉 `ApiError` 401；成功則 `access = str(token.access_token)`，因為 `ROTATE_REFRESH_TOKENS=True` 呼叫 `token` 上對應的 rotate 方法（或用 simplejwt 提供的方式）拿到新的 refresh，回應 body 只有 `{"access": ...}`，同時對這個新 refresh 重新 `set_cookie`（屬性跟 task 1.2 的 `GoogleLoginView` 一致，抽成共用函式或直接複製一份都可以，你判斷）。在 `apps/accounts/urls.py` 把 `refresh/` 從 `TokenRefreshView` 換成這個新的 `RefreshView`。讓 2.1 轉綠 — (auto) `pytest` 該檔全綠

## 3. Seam: `LogoutView` 改讀 cookie（對應需求「登出使後續刷新失效」「登出後瀏覽器不再保留刷新憑證」的 cookie 化）

- [x] 3.1 [RED] 滿足需求「登出撤銷 Session」「登出後瀏覽器不再保留刷新憑證」：修改既有的 logout 相關測試，改成用同一個 `APIClient` 實例登入（拿到 cookie）後直接打 `POST /api/auth/logout/`（**不在 body 帶 `refresh`**，依賴 cookie），驗證：① 205 ② response 有對 `refresh_token` 這個 cookie 下發刪除指令（`max_age=0` 或等效的過期設定）③ 撤銷後同一個 client 再打 `/api/auth/refresh/`（此時 client 保存的 cookie 應該已經因為 ② 被清掉，或即使還留著舊值也該被 blacklist）→ 401。也保留既有的「跨使用者不得撤銷」測試，改成從 cookie 角度出發（A 登入後手動把自己 client 的 `refresh_token` cookie 換成 B 的值，或用兩個 client 交換 cookie 值的方式模擬——你判斷怎麼寫最貼近「cookie 被竄改」這個情境）。確認先是 FAIL（目前的 `LogoutView` 還是讀 body）— (auto) `pytest` 顯示 FAIL
- [x] 3.2 [GREEN] 修改 `LogoutView`：從 `request.COOKIES.get("refresh_token")` 拿值取代原本讀 body 那段，其餘驗證/撤銷/跨使用者檢查邏輯不變；撤銷成功後額外 `response.delete_cookie("refresh_token", path="/api/auth/")`。讓 3.1 轉綠 — (auto) `pytest` 該檔全綠

## 4. CORS 設定與收尾

- [x] 4.1 在 `config/settings/base.py` 把 `CORS_ALLOW_CREDENTIALS` 設回 `True`（骨架階段拿掉過，這次因為要讓瀏覽器願意收送 cookie 重新需要）；順手確認 `CORS_ALLOWED_ORIGINS` 沒有被設成 `*`（本來就不該是，這裡是確認不是意外洩漏的設定） — (auto) `python manage.py check` 無錯，且 `grep CORS_ALLOWED_ORIGINS config/settings/base.py` 確認沒有寫死 `*`
- [x] 4.2 跑 `uv run pytest`（整個專案）、`uv run ruff check .`、`uv run python manage.py check`，三者皆需乾淨 — (auto) 三個指令 exit code 皆 0
- [x] 4.3 確認邊界：`git diff --stat develop...HEAD -- apps/events apps/recommendations apps/notifications` 應無輸出 — (auto) 指令輸出為空
