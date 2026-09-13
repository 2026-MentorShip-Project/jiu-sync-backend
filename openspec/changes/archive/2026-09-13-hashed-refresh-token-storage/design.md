## Context

目前 `refresh` token 的撤銷追蹤完全靠 `rest_framework_simplejwt.token_blacklist`：`OutstandingToken`（每筆登入/換發都建一筆，含明文 `token` 欄位）+ `BlacklistedToken`（標記撤銷）。`RefreshView`／`LogoutView` 目前呼叫套件提供的 `token.outstand()`／`token.blacklist()`，`RefreshToken()` 建構子在這個 app 安裝時會自動呼叫 `check_blacklist()` 做撤銷檢查。查過套件原始碼（`tokens.py`）確認 `check_blacklist()` 只用 `jti` 查表，完全不讀明文 `token` 欄位——這個欄位只是額外存的稽核資料，不是撤銷機制運作必要的東西。見 proposal.md - Why。

## Goals / Non-Goals

**Goals:**
- Postgres 裡不再有任何地方明文存放可直接使用的 refresh JWT 字串
- 撤銷/換發的對外行為（誰能不能換發、誰能不能登出）完全不變，只換底層儲存機制

**Non-Goals:**
- 不重寫 refresh token 本身的簽章/過期驗證——那段是 JWT 的密碼學驗證，跟這次「撤銷紀錄存哪」無關，繼續用 simplejwt 的 `RefreshToken()` 建構子
- 不處理 `access` token——它本來就無狀態，不查任何表，這次完全不碰
- 不做定期清理過期紀錄的排程任務——這個技術債在 `google-sso-login` 的 design.md 已經記過一次（當時是對 simplejwt 自己的 blacklist 表），現在换成對自己的新表，性質一樣，繼續留著當已知待辦，不在這次範圍內解決

## Decisions

**Hash 演算法：SHA-256，不加 salt。**
Salt 是為了防止「同樣明文（例如同一組常見密碼）雜湊出同樣結果」被預先算好的彩虹表攻擊——這是密碼雜湊要處理的問題，因為密碼本身熵不夠高、使用者常重複用同一組。Refresh token 不是這種東西：它是 JWT 函式庫用密碼學亂數產生的高熵字串，不存在「猜得到明文」的問題，加不加 salt 對安全性沒有實質差異，不加更簡單。

**同時存 `jti` 跟 `token_hash`，`jti` 當主要查詢鍵。**
`jti` 是 token payload 裡的隨機 ID，`RefreshToken()` 建構、驗證的過程本來就會解出這個值，用它查表最直接、跟 simplejwt 原本 `check_blacklist()` 的查詢方式一致。額外存 `token_hash`（SHA-256 hex digest，64 字元）是防禦縱深——多一層「這個 jti 對應的真的是這串 token」的交叉驗證，成本很低（多一個欄位、一次 hash 運算），沒有理由不順手做。

**`RefreshTokenRecord` model（放 `apps/accounts/models.py`，這個 app 既有的認證邊界內）：**
```python
class RefreshTokenRecord(models.Model):
    jti = models.CharField(max_length=64, unique=True, db_index=True)
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="refresh_tokens")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
```
`on_delete=CASCADE`（不是 simplejwt 原本用的 `SET_NULL`）：這個專案目前沒有刪除使用者的功能，一旦真的刪除使用者，他名下的 refresh 紀錄沒有再保留的意義，直接一起清掉比留著一堆 `user=NULL` 的孤兒紀錄乾淨。

**完全移除 `rest_framework_simplejwt.token_blacklist`，不是兩套並存。**
並存會讓「誰才是真正的撤銷判斷依據」變得不清楚，之後除錯容易搞混；這個專案還沒有真實使用者資料，移除 app、`migrate token_blacklist zero` 清表，沒有資料遺失風險。連帶移除 `SIMPLE_JWT["BLACKLIST_AFTER_ROTATION"]` 這個設定值——它只控制 simplejwt 自己 app 的行為，我們不再用那套機制，這個值留著會誤導人以為還在生效。

**驗證流程順序（`RefreshView`）：**
1. 從 cookie 拿 `refresh_token` 字串，沒有就 401
2. `hashlib.sha256(value.encode()).hexdigest()` 算出 hash，查 `RefreshTokenRecord`：查無、`revoked_at` 有值、或 `expires_at` 已過 → 401（不管哪種原因，統一 `INVALID_REFRESH_TOKEN`，不特別區分「不存在」跟「已撤銷」跟「過期」——這些對前端來說都是同一種處理方式：重新登入，沒有分開告知的必要）
3. 上一步查表通過後，才用 `RefreshToken(value)` 做 JWT 本身的簽章/格式驗證（`TokenError` 一樣轉 401）——**查表在前、JWT 驗證在後**：查表是純 DB 查詢、成本低，先擋掉明顯無效的請求；真的要驗證簽章這種較貴的密碼學運算，留給通過第一關的請求才做
4. 通過後：`access = str(token.access_token)`；rotate（`set_jti/set_exp/set_iat`，不再呼叫 `outstand()`／`blacklist()`）；把舊的 `RefreshTokenRecord` 標 `revoked_at=now()`；為新 token 建一筆新的 `RefreshTokenRecord`；`set_cookie` 新值

**`LogoutView` 一樣先查表拿到 `RefreshTokenRecord`，用 `record.user_id` 比對 `request.user.id`**（不用再解 JWT payload 拿 `user_id`，查表就順便拿到了，比之前的做法更直接）。

## Migration Plan

1. `python manage.py migrate token_blacklist zero`（清掉 simplejwt blacklist app 的表——這個專案沒有真實資料，安全操作）
2. `config/settings/base.py`：`INSTALLED_APPS` 移除該 app，移除 `BLACKLIST_AFTER_ROTATION`
3. `apps/accounts/models.py` 加 `RefreshTokenRecord`，`makemigrations accounts && migrate`
4. 改 view 邏輯

## Risks / Trade-offs

- **[新表一樣會長期累積過期紀錄]** → 跟之前 simplejwt 版本一樣的既知取捨（見 Non-Goals），沒有變得更差，也沒有變得更好，之後排清理任務時一併處理即可。
- **[自己實作撤銷檢查，少了套件維護者的既有測試覆蓋]** → 用同樣嚴謹的 TDD 流程補上等價的測試案例（有效／過期／已撤銷／不存在四種查表結果都要測到），把這個風險壓到最低。
