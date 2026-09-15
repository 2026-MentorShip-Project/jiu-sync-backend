## Context

`apps/events` 目前是空殼(`models.py`/`views.py`/`serializers.py` 皆未實作,`urls.py` 只有空的 `DefaultRouter()`)。既有可參考前例:`apps/accounts`(`User` UUID pk、JWT 認證、`ApiError`/自訂例外處理慣例、camelCase 欄位如 `idToken` 的命名前例)。權威 API 契約來自使用者提供的 Swagger 文件(2026-09-15/16 版,artifact `76e3cba4-eae7-4003-b30b-ce96789538d2`)與已完成的 grill-me 逐項確認記錄。詳細動機見 proposal.md - Why。

## Goals / Non-Goals

**Goals:**
- 三支 endpoint(建立活動、取得單一活動、取得自己活動清單)的資料模型、序列化格式、權限規則,與前端契約完全對齊
- `Event`/`Slot` 的 schema 設計要能承接未來的定案/取消/參與者投票功能,不需之後重新設計主鍵或關聯方式

**Non-Goals:**
- 不實作 `PATCH /api/events/{id}`、finalize/cancel、輪詢(`/live`)、參與者投票(`ParticipantResponse`)、AI 餐廳推薦——這些是未來 change 的範圍,本次僅在 model 層預留必要欄位或設計空間
- 不引入 Redis 防連點/去重機制

## Decisions

### D1. `Event`/`Slot` 使用 UUID 主鍵
比照既有 `User` model 的設計(不暴露可枚舉的循序 id)。不採用文件範例裡 `evt_xxx`/`slot_xxx` 這種自訂前綴字串 id——那只是文件示意寫法,契約本身只要求 `type: string`,DRF 序列化 UUID 欄位輸出的就是字串,符合契約且不需額外實作 id 產生器。

### D2. `Slot` 獨立成 model,不用 Event 上的 JSONField
未來 `ParticipantResponse` 需要用 through-model 記錄「每個參與者對每個候選時段的回覆狀態」(多對多+額外資料),`Slot` 必須有自己的 pk 才能被那張中介表 FK 參照。若這次用 JSONField 存時段,未來要遷移成正規化 model 是破壞性 migration;現在直接做成獨立 model,成本差異不大,但省掉未來一次資料遷移。

### D3. `displayStatus` 是 serializer 層的衍生欄位,不落地資料庫
理由與 `finalized_at`/`cancelled_at` 兩個新欄位的存在,已整理在 proposal.md 的 What Changes 與 specs/events/spec.md 的「活動顯示狀態衍生計算」需求中。這裡補充實作面:計算邏輯放在一個獨立的純函式(例如 `apps/events/lifecycle.py` 的 `compute_display_status(status, response_deadline, finalized_at, cancelled_at, now)`),不寫在 serializer 內聯邏輯裡,方便日後被定案/取消的 view 邏輯共用,也方便未來 `GET /events/{id}/live` 直接呼叫同一函式而不重複邏輯。

六態全部實作,即使本次資料庫只會產生 `voting_open`/`voting_closed_pending` 兩種:這是已與前端談定的正式 contract 值域(見 grill-me 決策記錄),值域對齊不算過度設計;其餘四態雖然本次無法透過任何 API 觸發,但函式本身可被單元測試直接覆蓋全部分支,不依賴資料庫真的產生對應狀態的資料。

### D4. 主揪 Email 遮罩邏輯放在 serializer,不放 view
`isOwner`(比對 `request.user == event.owner`)與 `hostEmail` 的遮罩規則(非擁有者一律回傳 `null`)都需要拿到目前請求者身分,透過 serializer 的 `context={"request": request}` 傳入,在 `SerializerMethodField` 內判斷。不在 view 層手動組字典回應,維持與既有 `apps/accounts` 的 DRF serializer 慣例一致。

### D5. `POST /api/events` 回應只回 `{id, shareUrl}`,`shareUrl` 由後端組
新增 `settings.FRONTEND_BASE_URL`(透過 `django-environ` 讀取):`dev.py` 預設 `http://localhost:5173`,`prod.py` 要求必填(比照現有 `DJANGO_SECRET_KEY`/`ALLOWED_HOSTS` 的 fail-fast 慣例,未設定時啟動即報錯,不給預設值)。`shareUrl = f"{settings.FRONTEND_BASE_URL}/events/{event.id}"`。由後端組的理由:分享連結代表「別人看到的網址」,前端若用 `window.location.origin` 自己組,本機開發環境分享出去的連結會是 `localhost`,沒有意義;後端已知道正式對外網域,單一處維護即可。

### D6. `GET /api/events/{id}` 的 `responses` 欄位這次固定回傳空陣列
`ParticipantResponse` model 本次不建立(scope 排除項目)。序列化時 `responses` 欄位直接回傳 `[]`(hardcode 的 `SerializerMethodField`,而非查詢某個空 queryset),`EventSummarySerializer` 的 `responseCount` 同理固定回傳 `0`。不為了讓這兩個欄位「看起來像真的查詢結果」而提前建立 `ParticipantResponse` model——那會把本次 scope 擴大到參與者投票功能,不是本次要解決的問題。

### D7. camelCase 對外欄位命名
沿用 `apps.accounts.serializers.GoogleLoginSerializer` 的 `idToken` 前例。專案目前沒有全域的 camelCase renderer,本次在這兩個新 serializer 上以 DRF 欄位 `source=` 手動映射(例如 `hostNickname = serializers.CharField(source="host_nickname")`),不新增全域轉換套件——影響範圍限定在這次新增的欄位,不影響既有 `apps.accounts` 的 serializer 行為。

### D8. 驗證失敗與查無資料沿用既有錯誤處理
不新增錯誤處理機制。驗證失敗用 DRF 內建 `serializers.ValidationError`,已被 `config/exceptions.py` 的 `custom_exception_handler` 統一包裝成 `{"message", "code"}` 格式。`GET /api/events/{id}` 查無資料時,view 用 `get_object_or_404` 讓既有 `handler404` 處理,不手動拋 `Gone`(410)——410 語意是「連結已失效」,本次沒有任何邏輯會讓一筆已存在的資料查詢後才被判定失效,單純不存在就是 404。

## Risks / Trade-offs

- **[風險] `finalized_at`/`cancelled_at`/`final_slot_id`/`final_note` 四個欄位這次加進 model 但沒有任何寫入路徑,存在期間內只會是 `null`** → 緩解:這是刻意的 schema 前置決策(見 grill-me 決策),換取未來定案/取消 change 不需要 migration 就能直接使用;有在 design.md/proposal.md 明確記錄動機,避免日後被誤認為忘記實作。
- **[風險] `displayStatus` 六態中有四態這次無法被任何 API 路徑觸發,測試只能走純函式單元測試,無法用整合測試涵蓋** → 緩解:D3 已將計算邏輯抽成獨立純函式,tasks.md 會針對這個函式寫涵蓋六個分支的單元測試,不依賴 API 整合測試。
- **[風險] `EventSummarySerializer.responseCount` 固定回傳 0,語意上「看起來像」有實際計數邏輯** → 緩解:D6 已記錄這是刻意的暫時行為,待 `ParticipantResponse` model 出現的 change 再改為真實 annotate 計數,不在本次強行做假查詢。

## Migration Plan

新增 `Event`、`Slot` 兩張表的 migration,屬於全新表,無既有資料遷移風險,可直接 `python manage.py makemigrations events && python manage.py migrate`。若需回滾,`migrate events <前一版 migration 編號>` 即可,兩張表尚無外部資料依賴。
