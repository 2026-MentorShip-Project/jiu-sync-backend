> TDD 排法：RED（先寫會失敗的測試）→ GREEN（只寫剛好讓它通過的最小實作）。四個 seam 皆走 HTTP 層測（DRF test client）。
>
> 這個 change 不引入新工具鏈／新依賴（沿用既有 Django/DRF），Seam 1／3 各需要一個新 migration（新 model），這是既有工具鏈下的新 schema，不算「新工具鏈或新依賴」，不需要獨立的環境健檢 task，直接併入對應 seam。
>
> 每項驗證方式前綴 `(auto)`/`(manual)`：全部是後端 API，全部可由 `pytest`/`ruff`/`manage.py` 指令自動驗證，沒有需要人工操作確認的步驟。

## 1. Seam: `POST /api/events/{id}/responses`（對應 spec 需求：參與者初次投票）

- [x] 1.1 [RED] 新增 `ParticipantResponse` model（`apps/events/models.py`）：`id`（CharField，primary_key、max_length=8、`default=generate_short_id`、`editable=False`，重用既有 `apps/events/ids.generate_short_id`，與 `Event.id` 同款、非 UUID）、FK `Event`（`related_name="responses"`）、`nickname`（CharField max_length=40）、`phone_last_three_hash`（CharField）、`email`（EmailField null/blank）、`slots`（`ManyToManyField(Slot, related_name="responses")`）、`created_at`/`updated_at`；`Meta.unique_together = ("event", "nickname")`。跑 `makemigrations` 產生 migration。在 `apps/events/tests/test_views.py` 補測試，涵蓋：① 合法輸入（暱稱＋手機末三碼＋複選 2 個時段）→ 201，回應含新建 response 的 `id`（8 碼 base62 格式），DB 有一筆對應資料，`phone_last_three_hash` 不等於明碼、且能透過 `check_password` 驗證回原始輸入；② 選填 `email` 不帶 → 201，`email` 為 null；③ 暱稱與既有（trim 後）重複 → 400 `NICKNAME_TAKEN`，DB 未新增；④ 暱稱前後帶空白但 trim 後與既有重複 → 400 `NICKNAME_TAKEN`；⑤ 暱稱缺漏／手機末三碼缺漏／`selectedSlotIds` 空陣列 → 400 對應 code；⑥ 手機末三碼非 3 位數字（帶字母、2 位、4 位）→ 400 `PHONE_LAST_THREE_INVALID`；⑦ `selectedSlotIds` 內含不屬於該活動的 slot id → 400 `SLOT_NOT_FOUND`；⑧ 活動不存在 → 404 `EVENT_NOT_FOUND`；⑨ 活動連結已失效（`status=finalized`/`cancelled` 超過 7 天，直接建測試資料）→ 410 `LINK_EXPIRED`；⑩ 活動 `status` 非 active（取消／定案，未超過 7 天）→ 409 `EVENT_NOT_ACTIVE`；⑪ 活動 `status=active` 但 `response_deadline` 已過 → 409 `VOTING_CLOSED`；⑫（D9）用 `unittest.mock.patch` 讓 `generate_short_id` 前兩次回傳同一個已存在的 id、第三次回傳新 id → 仍 201 成功建立（驗證碰撞重試路徑）；另外驗證兩個不同暱稱正常各自成功時不會誤觸發重試。確認這組測試現在是 FAIL（view/model 都還不存在）— (auto) `pytest` 顯示這幾條 FAIL
- [x] 1.2 [GREEN] 實作：`apps/events/serializers.py` 新增 `ParticipantResponseCreateSerializer`（`nickname`/`phoneLastThree`/`email`/`selectedSlotIds`，`validate_nickname` 內 trim；`validate_phoneLastThree` 檢查 3 位數字格式；`validate_selectedSlotIds` 檢查非空且皆屬於該活動的 slot id 集合）；`apps/events/views.py` 新增 `ParticipantResponseCreateView` 與三支端點共用的前置條件檢查函式（D6：存在→連結未失效→狀態，供 Seam 3/4 重用）；`create()`（D9）比照 `EventCreateSerializer.create()` 的碰撞重試迴圈寫法，`phone_last_three_hash` 用 `django.contrib.auth.hashers.make_password` 產生，`transaction.atomic()` 包住 `ParticipantResponse.objects.create()` + `slots.set()`，捕獲 `IntegrityError` 後先查 `ParticipantResponse.objects.filter(event=event, nickname=nickname).exists()`：存在則回 400 `NICKNAME_TAKEN`（不重試）；不存在則視為 id 碰撞，重試（沿用與 `EVENT_ID_COLLISION_MAX_ATTEMPTS` 同款重試次數上限，可另定義一個對應常數）；`apps/events/urls.py` 新增路由；`config/exceptions.py` 的 `FIELD_CODE_OVERRIDES` 補上本次新增欄位的 code。讓 1.1 全部轉綠為止，不多加東西 — (auto) `pytest apps/events/tests/test_views.py` 該檔全綠

## 2. Seam: `GET /api/events/{id}` 回傳真實彙整結果（對應 spec 需求：修改「查詢單一活動完整資料」使 `responses` 真實回傳）

- [x] 2.1 [RED] 補測試：① 活動有 2 筆投票 → `responses` 陣列含 2 筆，各自 `nickname`/`selectedSlotIds` 正確；② 活動無任何投票 → `responses` 為空陣列（既有行為維持）；③ 確認回應不含 `phoneLastThree`/`email` 欄位。確認 FAIL（目前 `get_responses` 仍寫死空陣列）— (auto) `pytest` 顯示 FAIL
- [x] 2.2 [GREEN] 實作：`EventDetailSerializer.get_responses()` 改為查詢 `event.responses.all()`（序列化成 `[{"id":..., "nickname":..., "selectedSlotIds":[...]}]`）；`EventDetailView.get()` 的 queryset 補上 `prefetch_related("responses__slots")` 避免 N+1。讓 2.1 全部轉綠 — (auto) `pytest` 全綠

## 3. Seam: `POST /api/events/{id}/responses/verify`（對應 spec 需求：參與者身分核對）

- [x] 3.1 [RED] 新增 `ParticipantResponseAccessToken` model：UUID PK、FK `ParticipantResponse`（`related_name="access_tokens"`）、`token_hash`（CharField unique db_index）、`expires_at`（DateTimeField）、`used_at`（DateTimeField null/blank）、`created_at`。跑 `makemigrations`。補測試，涵蓋：① 正確暱稱＋正確手機末三碼 → 200，回應含 `accessToken`（明文）、`expiresAt`、原投票內容（`nickname`/`email`/`selectedSlotIds`，供前端預填）；DB 新增一筆 token 紀錄，`token_hash` 不等於明碼 `accessToken`；② 暱稱不存在 → 401 `IDENTITY_VERIFICATION_FAILED`；③ 暱稱存在但手機末三碼錯誤 → 401 `IDENTITY_VERIFICATION_FAILED`（與②回應 body 完全相同，驗證不洩漏差異）；④ 活動不存在／連結失效／狀態非 active／投票已截止 → 沿用 Seam 1 的共用前置檢查函式，各補一條測試。確認 FAIL — (auto) `pytest` 顯示 FAIL
- [x] 3.2 [GREEN] 實作：`ParticipantResponseVerifySerializer`（`nickname`/`phoneLastThree`）；`ParticipantResponseVerifyView`：查 `event.responses.filter(nickname=trimmed)`，查無或 `check_password` 失敗皆回同一個 401 `IDENTITY_VERIFICATION_FAILED`（D3）；核對成功用 `secrets.token_urlsafe` 產生明文 token，`hashlib.sha256` 雜湊後存 `token_hash`，`expires_at = now + 30min`；回應回傳明文 token（僅此一次）。`apps/events/urls.py` 新增路由。讓 3.1 全部轉綠 — (auto) `pytest` 全綠

## 4. Seam: `PATCH /api/events/{id}/responses/{responseId}`（對應 spec 需求：參與者更新投票）

- [x] 4.1 [RED] 補測試，涵蓋：① 帶有效未過期未使用的 token，修改 `selectedSlotIds` → 200，DB 該筆投票的 slots 已更新為新集合，`nickname`/`email`/`phone_last_three_hash` 皆未變動；② token 使用後再次帶同一個 token 送出 → 401 `ACCESS_TOKEN_INVALID`（一次性驗證）；③ token 已過期（直接建立 `expires_at` 為過去的測試資料）→ 401 `ACCESS_TOKEN_INVALID`；④ token 不存在／格式錯誤 → 401 `ACCESS_TOKEN_INVALID`；⑤ token 屬於另一筆 response，拿來改這筆的 `responseId` → 401 `ACCESS_TOKEN_INVALID`；⑥ body 帶 `nickname`/`email`/`phoneLastThree` 企圖修改 → 皆被忽略，DB 對應欄位不變（僅 `selectedSlotIds` 生效）；⑦ `selectedSlotIds` 含不屬於該活動的 slot id → 400 `SLOT_NOT_FOUND`，DB 未變動，token 未被消費；⑧ 活動連結已失效／狀態非 active／投票已截止 → 沿用共用前置檢查，各補一條測試，token 未被消費。確認 FAIL — (auto) `pytest` 顯示 FAIL
- [x] 4.2 [GREEN] 實作：`apps/events/urls.py` 的 `responseId` 路徑參數重用既有 `shortid` converter（`ShortIdConverter`，見 `EventDetailView` 路由既有寫法），不是 `<uuid:responseId>`——`ParticipantResponse.id` 是 8 碼 base62 短 id(D9),不是 UUID;`ParticipantResponsePatchSerializer`（僅宣告 `selectedSlotIds`，不宣告 `nickname`/`email`/`phoneLastThree`，沿用專案既有「未宣告欄位自動被忽略」慣例）；`ParticipantResponseDetailView.patch()`：先驗證 token（`hashlib.sha256` 雜湊比對 `token_hash`，檢查 `expires_at > now`、`used_at is None`、關聯的 `response.id == responseId`，任一不符皆回 401 `ACCESS_TOKEN_INVALID`），通過共用前置條件檢查與 slot 驗證後，在 transaction 內更新 `slots` M2M 並將 token `used_at` 設為 now（一次性消費）；前置條件／slot 驗證失敗的路徑不消費 token。讓 4.1 全部轉綠 — (auto) `pytest` 全綠

## 5. 收尾

- [x] 5.1 跑 `uv run ruff check .`、`uv run python manage.py check`、`uv run pytest`(全套)，確認三者皆乾淨無誤 — (auto) 三個指令 exit code 皆 0
- [x] 5.2 驗證邊界需求「未涵蓋範圍不得被誤實作」：確認沒有加入手機末三碼失敗次數鎖定／rate limit、沒有讓 `PATCH .../responses/{id}` 接受修改 `email`/`nickname` — (auto) `git diff --stat feature/error-code-table...HEAD -- apps/events` 顯示的檔案清單與異動內容不含上述項目

## 6. commit 後追加需求（留言欄位／主揪暱稱衝突／VOTING_CLOSED 狀態碼，design.md D10/D11/D12/D13）

> 這輪未嚴格先紅後綠——程式碼與測試撰寫順序顛倒，已記錄於
> `docs/agents/incident-log/2026-09-21.md`，測試最終仍全數涵蓋並通過。

- [x] 6.1 `ParticipantResponse` 新增 `comment`（CharField，max_length=200，null/blank）欄位與 migration；`ParticipantResponseCreateSerializer` 新增 `comment`（選填，`allow_blank=True`，空字串正規化成 `None`）；`validate_nickname` 新增與 `event.host_nickname` 精確比對（trim 後），相同時拒絕並回 `NICKNAME_CONFLICTS_WITH_HOST`；`_check_participation_preconditions` 新增 `voting_closed_status_code` 參數（預設 409），`ParticipantResponseCreateView` 傳入 400；`config/exceptions.py` 補 `("comment", "max_length")`。補測試：留言可正常儲存／不帶留言為 null／空字串留言正規化為 null／超長 400 `COMMENT_TOO_LONG`；暱稱與主揪暱稱相同（含 trim 後相同）→ 400 `NICKNAME_CONFLICTS_WITH_HOST`；投票已截止 → 400（不是 409）`VOTING_CLOSED`，且更新既有兩則因暱稱誤撞新規則而失敗的測試（碰撞重試、不同暱稱不誤觸發重試）改用不會與主揪暱稱衝突的測試暱稱。`verify`/`PATCH` 兩支端點的 `VOTING_CLOSED` 測試維持 409，未受影響 — (auto) `pytest`（全套 154 passed）、`ruff check`、`manage.py check` 皆乾淨
- [x] 6.2 使用者確認 `VOTING_CLOSED` 統一改回 409（撤回 6.1 的 400 不對稱）：移除 `voting_closed_status_code` 參數，`_check_participation_preconditions` 固定回 409；同步修正 spec.md/測試。另外處理 Codex 二次審查兩項發現（design.md D13）：① `PATCH .../responses/{responseId}` 的 token compare-and-swap 補上 `expires_at__gt=<CAS 當下重新取得的 now>`，修掉早期檢查與真正消費之間的極短空檔可能讓過期 token 仍成功消費的落差，補一則用真實時間流逝（人為延遲注入）驗證的測試，修正前先確認會 FAIL；② `ParticipantResponseAccessToken` 無清除策略記入 design.md Risks，非本次 blocker，不動程式碼 — (auto) `pytest`（全套 155 passed）、`ruff check`、`manage.py check` 皆乾淨
- [x] 6.3 用真實請求實測畸形 request body 時發現：`selectedSlotIds` 元素非合法 UUID 字串 → 回應 `code: "invalid"`（DRF 原始碼，未語意化）。已與使用者確認補上 `FIELD_CODE_OVERRIDES` 的 `("selectedSlotIds", "invalid"): "SLOT_ID_INVALID"`（同時涵蓋 create／patch 兩支端點，欄位名相同）；`POST .../responses`、`PATCH .../responses/{responseId}` 各補一則測試（RED 先確認會拿到未對照的 `"invalid"`，補表後轉綠）；spec.md 兩個 Requirement 補上 `SLOT_ID_INVALID` 的 Scenario 與修訂記錄（design.md D14）— (auto) `pytest`（全套 157 passed）、`ruff check`、`manage.py check` 皆乾淨

## 7. commit 後追加需求（候選時段改三態表態，design.md D4 2026-09-21 修訂／D4a）

> 使用者事後明確要求候選時段從二元複選改為 `available`/`if_needed`/`unavailable`
> 三態，取代 task 1-6 已完成並 commit 的二元複選設計——已透過 AskUserQuestion
> 確認四個關鍵決策點（enum 命名、body 結構、是否須涵蓋全部候選時段、GET 彙整頁
> 是否跟著改),詳見 design.md D4/D4a。

- [x] 7.1 `ParticipantResponse.slots` 從 plain `ManyToManyField` 改為帶
  `through="ParticipantResponseSlotAvailability"` 的 M2M；新增
  `ParticipantResponseSlotAvailability` model（`response`/`slot` FK 皆
  CASCADE，`availability` CharField choices，`unique_together (response,
  slot)`）。因 Django 不支援 plain M2M 直接 `AlterField` 成帶 through 的 M2M
  （schema editor 會拋 `ValueError: ... not compatible types`），刪除
  task 1-6 累積的 4 個舊 migration（0003-0006，皆未合併到 main/develop、只存在
  這條未合併的 feature branch），重新產生單一乾淨的 migration。三支序列化器
  （create/verify/patch）與 `GET` 的 `get_responses()` 全數改用
  `slotAvailabilities: [{slotId, availability}, ...]`（新增
  `SlotAvailabilityInputSerializer`、共用的 `_validate_slot_availabilities`
  同時檢查「屬於該活動」與「恰好涵蓋全部候選時段各一次」）；PATCH 的寫入策略
  改為「先刪除該筆投票既有的全部表態列、再整批重建」。`config/exceptions.py`
  新增 `SLOT_AVAILABILITIES_REQUIRED`／`SLOT_ID_INVALID`／`AVAILABILITY_INVALID`
  對照。改寫全部受影響測試（helper `_create_participant_response`/
  `_response_payload` 改參數化支援三態；新增 exhaustiveness 相關測試：缺漏
  時段、重複時段、`availability` 非法值）— (auto) `pytest`（全套 161
  passed）、`ruff check`、`manage.py check`、`makemigrations --check
  --dry-run` 皆乾淨
- [x] 7.2 過程中發現並修正兩個非預期的既有/新落差：① `config/exceptions.py`
  的 `_build_errors` 對「`ChildSerializer(many=True)` 巢狀驗證錯誤」的形狀
  假設錯誤——原本以為是 list（成功索引補空 dict 佔位），實測確認 DRF 3.18
  實際回傳的是「以索引為 key 的 dict、只有失敗索引才出現」，導致這條巢狀
  code 對照（`slots[].date` 等）自 `add-events-api` 以來從未真的生效過，一律
  fallback 成 DRF 原始 code；修正判斷條件，補回歸測試證明
  `slots[0].label` 現在真的能拿到 `SLOT_LABEL_TOO_LONG`，並修正
  `config/tests/test_exceptions.py` 裡一則用同款錯誤假設手寫的測試 fixture。
  ② `test_participant_can_submit_first_vote_successfully` 用
  `event.slots.first()` 在已呼叫 `_add_slot()` 新增第二個 slot 之後才取值，
  `Slot.id` 是 UUID、`.first()` 無 `order_by` 時不保證回傳最初建立的那筆，
  導致測試偶發真正選到兩個「不同名字、相同 id」的 slot（重複 id 被新的
  exhaustiveness 檢查攔下）——改成在新增第二個 slot 之前先取值 — (auto)
  `pytest`（全套 161 passed）皆乾淨
- [x] 7.3 commit 後再追加需求（design.md D16）：`POST .../responses`／
  `PATCH .../responses/{responseId}` 回應改回傳完整 `GET /api/events/{id}`
  格式（不再是極簡的 `{id}`／`{id, slotAvailabilities}`），寫入完成後重新以
  `_event_with_responses_queryset()` 查一次事件並用 `EventDetailSerializer`
  序列化；`GET` 的 `responses` 彙整正式加入 `comment`（反轉 D12「本次不做
  顯示」的決定）。事涉對外回應契約，先走 grill-me 確認三個問題（回應要多
  完整／comment 要不要顯示／verify 端點要不要一併改），使用者三題皆選
  Recommended：完整 GET 格式、加入 comment、verify 維持現狀不改。同步更新
  受影響測試（`test_participant_can_submit_first_vote_successfully` 等）—
  (auto) `pytest`（全套 161 passed）、`ruff check`、`manage.py check`、
  `makemigrations --check --dry-run` 皆乾淨
- [x] 7.4 commit 後再追加需求（design.md D17）：新增頂層 `slotSummary`
  欄位，回傳每個候選時段的三態票數（`available`/`if_needed`/`unavailable`
  各自整數），供前端呈現「這個時段幾個人可以」，之後才做「誰投了哪個時段」
  （既有的 `responses[].slotAvailabilities` 已涵蓋，不需要另外設計）。事涉
  新增對外回應欄位，先走 grill-me 確認三個問題（放哪裡／算什麼／key
  命名），使用者三題皆選 Recommended：獨立頂層陣列（不塞進 `slots[]`）、只拆
  三態原始票數（不額外算合計）、key 直接沿用 `available`/`if_needed`/
  `unavailable`（不另外設計 camelCase 命名）。`EventDetailSerializer.
  get_slotSummary()` 用已 prefetch 的資料在 Python 端累加，不下額外 query；
  `_event_with_responses_queryset()` 補上 `slots` 的 prefetch。RED 先補
  `test_event_detail_responses_field_is_empty_list_when_no_votes`（全為 0）、
  `test_event_detail_responses_field_contains_real_votes`（混合三態票數）、
  `test_participant_can_submit_first_vote_successfully` 的完整回應格式斷言
  三處，確認 FAIL 後才實作 — (auto) `pytest`（全套 161 passed）、
  `ruff check`、`manage.py check`、`makemigrations --check --dry-run` 皆乾淨
