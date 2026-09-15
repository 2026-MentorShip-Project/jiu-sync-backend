## Why

`rest_framework_simplejwt.token_blacklist` 的 `OutstandingToken.token` 是 `TextField()`，每次登入／換發都把**完整、可直接使用的原始 refresh JWT 字串明文寫進 Postgres**。查過套件原始碼確認：撤銷檢查（`check_blacklist()`）只靠 `jti`（token 裡的隨機 ID）查表，從頭到尾都不需要用到這個明文欄位——它純粹是多存的風險，不是機制運作必要的東西。誰能讀到這張表（DB dump 外洩、之後任何一個 SQL injection、內部人員誤用），就能直接拿一個現成、還沒過期的 refresh token 冒充任何人。已跟使用者確認：不繼續用 simplejwt 內建的 blacklist app，自己實作一張只存 hash 的表。

## What Changes

- 新增 `RefreshTokenRecord` model（`apps/accounts`）：存 `jti`、`token_hash`（SHA-256 hex digest，不是明文）、`user`、`expires_at`、`revoked_at`（nullable，有值代表已撤銷）
- `rest_framework_simplejwt.token_blacklist` 從 `INSTALLED_APPS` 移除，連同它的表一起 migrate 掉（這個專案還沒有真實使用者資料，無遷移成本）
- `GoogleLoginView`／`RefreshView`／`LogoutView` 改用這張自己的表做「核發登記」「撤銷檢查」「撤銷」，不再呼叫 simplejwt blacklist app 提供的 `token.blacklist()`／`token.outstand()`／自動 `check_blacklist()`
- Refresh token 本身的簽章驗證（audience 不適用於 refresh，但過期/格式驗證仍是）繼續交給 simplejwt 的 `RefreshToken()` 建構子做，這部分完全不受影響——這次只換掉「撤銷紀錄怎麼存」，不重寫 JWT 驗證邏輯

## Capabilities

### New Capabilities

（無）

### Modified Capabilities

（無——這次是純內部儲存機制替換，`Session 刷新`／`登出撤銷 Session` 的對外行為〔誰能不能換發、誰能不能登出、回傳什麼錯誤碼〕完全不變，只是換掉底層怎麼追蹤撤銷紀錄。依規則這種「實作可以改、外部可觀察行為不變」的情況不寫 spec delta，`.openspec.yaml` 設 `skip_specs: true`）

## Impact

- `apps/accounts/models.py`：新增 `RefreshTokenRecord`
- `apps/accounts/migrations/`：新的 migration（新增 `RefreshTokenRecord` 表）
- `config/settings/base.py`：`INSTALLED_APPS` 移除 `rest_framework_simplejwt.token_blacklist`；`SIMPLE_JWT["BLACKLIST_AFTER_ROTATION"]` 這個設定值連帶失去意義（只影響 simplejwt 自己的 blacklist 機制），一併移除
- `apps/accounts/views.py`：`GoogleLoginView`（登入核發時登記一筆）、`RefreshView`（查表決定要不要放行、rotate 時撤銷舊的登記新的）、`LogoutView`（查表決定歸屬、撤銷）全部要改
- 需要一次性 migrate：`python manage.py migrate token_blacklist zero` 移掉舊表，再套用新 migration
- 不影響 `apps/events`、`apps/recommendations`、`apps/notifications`，也不影響 `access` token 的簽發/驗證（那段完全獨立，不碰）
