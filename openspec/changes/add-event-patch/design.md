## Context

`apps/events` 已有 `Event`/`Slot` model、`POST /api/events`、`GET /api/events/{id}`、`GET /api/events?owner=me`(來自 `add-events-api`,尚未 merge/archive,本 change 建立在其分支 `feature/events-api-crud` 之上)。`Event` model 的六個可改欄位(`title`/`description`/`location`/`host_nickname`/`host_email`/`response_deadline`)已存在,不需 migration。`GET /api/events/{id}`(`EventDetailView`)目前用 `AllowAny` + 自訂 `OptionalJWTAuthentication`(無效/過期 token 視為匿名而非 401,見 `add-events-api` 的 Codex review 修正)。詳細動機見 proposal.md - Why。

## Goals / Non-Goals

**Goals:**
- `PATCH /api/events/{id}` 讓活動擁有者能編輯六個基本欄位,partial update 語意
- 沿用既有欄位驗證規則(長度、CJK 加權、時間必須晚於當下),不重寫一套新規則

**Non-Goals:**
- 不允許編輯 `mode`、`slots`——候選時段建立後即固定,這是產品設計邊界,不是這次技術限制
- 不做併發衝突偵測(樂觀鎖/版本號),採 last-write-wins
- 不實作 finalize/cancel 端點本身——僅在權限檢查裡預留「僅 active 可編輯」的邊界

## Decisions

### D1. PATCH 掛在 `EventDetailView`,依 HTTP method 分派不同認證/權限
`GET /api/events/{id}` 是公開端點(`AllowAny` + `OptionalJWTAuthentication`,無效 token 視為匿名),但 `PATCH` 需要嚴格 `IsAuthenticated` + 擁有者檢查。同一個 view class 若整組共用 `permission_classes`/`authentication_classes`,無法讓兩個 method 各自套用不同規則。做法:覆寫 `get_permissions()`/`get_authenticators()`,依 `self.request.method` 回傳不同組合——`GET` 沿用現有寬鬆規則,`PATCH` 回傳 `[IsAuthenticated()]` + 全域預設的 `JWTAuthentication`(不是 `OptionalJWTAuthentication`,無效 token 在 PATCH 語境下就該真的 401,不該被吞成匿名)。

延續 `add-events-api` 已建立的「多個 HTTP verb 共用同一個 view class、同一個 URL 掛載點」慣例(`EventListView`/`EventCreateView` 的繼承模式),這次是同一慣例套用到 GET+PATCH 這個組合,不是新架構。

替代方案:PATCH 另開一個獨立 view class、掛在同一個 URL——會需要額外處理兩個 view class 如何共用同一個 `as_view()` 掛載點(`urls.py` 一個路徑只能對應一個 view),沒有 `get_permissions()` 分派乾淨。

### D2. 擁有者檢查回 403,不是 404
`GET /api/events/{id}` 本身公開,活動是否存在不是秘密。PATCH 若對非擁有者回 404(假裝查無此活動),語意上會跟「這個 id 明明能被任何人 GET 到」矛盾。403(資源存在、但你沒權限改)更貼切,也是本次 grill-me 已確認的決策。

### D3. `hostEmail` 與登入帳號 Email 脫鉤
`add-events-api` 原本鎖定「`host_email` 只能由後端在建立時自動代入 `request.user.email`,PATCH 尚未存在,故無法變更」。本次確認翻案:`hostEmail` 是「活動聯絡信箱」,建立時預設帶入帳號 Email 只是初始值,PATCH 之後允許改成任意格式合法的其他 Email,不再與帳號綁死。驗證僅套用標準 `EmailField` 格式檢查,不比對是否等於 `request.user.email`。

### D4. 成功回應回傳完整 `EventDetailSerializer`,不是精簡確認
`POST /api/events` 回應故意精簡(`{id, shareUrl}`),因為建立後前端導去分享頁不需要整包資料。PATCH 情境不同——使用者在編輯頁改完欄位,預期立刻看到更新後的畫面,直接複用 `EventDetailSerializer`(與 `GET /api/events/{id}` 同序列化器/同格式)讓前端一次拿到最新完整狀態,不必多打一次 GET。

### D5. 不加併發鎖(樂觀鎖/版本號)
情境是「單一主揪自己多分頁/多裝置編輯自己的活動」,不是多人協作同一活動,衝突機率低、後果也輕(頂多蓋掉自己剛剛另一個修改)。若之後出現共同主揪協作編輯需求,屆時再評估加樂觀鎖(例如比對 `updated_at`,不符回 409)。

### D6. 僅 `status=active` 可編輯,提前立邊界
`add-events-api` 範圍內沒有 finalize/cancel 端點,正常路徑不可能產生 `finalized`/`cancelled` 的活動,這條檢查目前不會被真實資料觸發。提前寫上是因為:等未來 finalize/cancel change 做出來時,這條防護已經就位,不需要回頭在那次 change 裡才補這個邊界(那時候反而容易漏掉,因為焦點會在 finalize 邏輯本身)。

### D7. 驗證失敗整包拒絕,不部分套用
沿用 DRF serializer 標準行為與既有 `config/exceptions.py` 錯誤格式——這不是新機制,只是重申既有慣例延續適用於 PATCH。

## Risks / Trade-offs

- **[風險] `status != active` 檢查目前無法被任何真實資料路徑觸發(finalize/cancel 端點不存在)** → 緩解:單元測試直接用 `status="finalized"`/`"cancelled"` 手動建立測試資料驗證這條分支,不依賴真的走過 finalize 流程產生資料。
- **[風險] 不加併發鎖,多裝置同時編輯可能互相覆蓋** → 緩解:已記錄為 D5 的刻意決策,影響範圍限於使用者編輯自己的活動、後果輕微,非本次要解決的問題。
- **[風險] `hostEmail` 脫鉤後,若活動擁有者的 Google 帳號本身換了 Email,不會自動同步活動的聯絡 Email** → 這是脫鉤決策的自然結果,不是缺陷:`hostEmail` 一旦被 PATCH 修改過,語意上就是「活動專屬聯絡信箱」,不再跟隨帳號變動,符合 D3 的設計意圖。

## Migration Plan

不需要 migration——六個可改欄位皆已存在於 `Event` model(`add-events-api` 已建立)。純新增一個 view method + 一個 serializer,無資料庫結構變動。若需回滾,移除 `patch()` 方法與對應 URL 分派即可,不影響既有 GET/POST/LIST 行為。
