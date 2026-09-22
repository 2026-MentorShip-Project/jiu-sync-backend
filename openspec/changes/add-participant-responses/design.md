## Context

`apps/events` 目前有 `Event`/`Slot` model，`POST /api/events`（建立）、`GET /api/events/{id}`（查詢，`responses` 欄位寫死空陣列）、`PATCH /api/events/{id}`（主揪編輯基本資訊）、`GET /api/events?owner=me`（主揪清單）。錯誤格式已統一為 `{message, code, errors[]}`（本 change 建立在 `feature/error-code-table` 分支之上）。`compute_display_status`（`apps/events/lifecycle.py`）已能判斷活動連結是否失效（`link_expired`）。詳細動機見 proposal.md - Why。

## Goals / Non-Goals

**Goals:**
- 參與者能透過分享連結初次投票、事後修改投票
- 唯讀彙整頁（`GET /api/events/{id}`）真實顯示投票結果
- 身分核對與修改投票分成兩支 API，中間以短期 token 銜接，不用每次修改都重新輸入暱稱＋手機末三碼

**Non-Goals:**
- 不做手機末三碼暴力破解防禦（失敗次數限制、鎖定、IP rate limit）——已與使用者確認為本次刻意排除的風險，見 Risks
- 不允許更新投票時修改 Email 或暱稱
- 不做投票的樂觀鎖／並發衝突偵測（見 D7）
- 不實作前端頁面本身，僅交付後端 API

## Decisions

### D1. 手機末三碼：`django.contrib.auth.hashers.make_password`/`check_password`，不加防暴力破解機制

只有 1000 種組合，即使用慢雜湊，已知暱稱的情況下，對外可見的核對 API 本身就是一個 oracle，1000 次請求即可窮舉——這不是雜湊演算法能解決的問題，而是輸入熵太低的先天限制。已與使用者確認：本次刻意不加失敗次數限制／鎖定機制，接受此風險，不在本次 scope 內處理。選 `make_password`（PBKDF2＋內建 per-record salt）而非手寫 `hashlib.sha256`（`RefreshTokenRecord` 的既有作法）——後者用在高熵 token 上，token 本身不可窮舉不需要 salt；手機末三碼熵極低，至少要有 per-record salt 防止「一份全域彩虹表打天下」的離線批次查表，`make_password` 是 Django 內建、免手動管理 salt 欄位的標準作法。

### D2. 更新投票的身分核對與修改分成兩支 API，以 DB 儲存的一次性 token 銜接

`POST /api/events/{id}/responses/verify` 核對成功後建立 `ParticipantResponseAccessToken`（明文 token 只在這次回應回傳，DB 只存 `hashlib.sha256` 雜湊值，比照 `RefreshTokenRecord` 的既有作法——token 本身熵夠高，不需要 `make_password`），效期 30 分鐘，成功用過一次（`PATCH` 消費）即標記失效，不可重複使用。`PATCH` 帶著這個 token 而非重新帶暱稱＋手機末三碼，前端不需要在修改頁面重複保存敏感輸入。Token 過期或已使用，`PATCH` 一律回 401 `ACCESS_TOKEN_INVALID`，前端導回重新核對——不細分「過期」與「已使用」等原因，處理方式相同。

替代方案：每次 `PATCH` 都重新帶暱稱＋手機末三碼——更簡單、不用新表，但前端要嘛每次都重新彈 Modal 輸入（規格描述是核對一次後「開放修改」，暗示核對後有一段可連續操作的視窗），要嘛把敏感資料暫存在前端（不安全）。已與使用者確認採 token 方案。

### D3. 身分核對失敗訊息統一，不區分暱稱不存在或手機碼錯誤

`POST .../verify` 核對失敗時，不論是「該活動下查無此暱稱」或「暱稱存在但手機末三碼不符」，一律回同一個 401 `IDENTITY_VERIFICATION_FAILED`，避免錯誤訊息本身變成一個「暱稱是否存在」的 side channel——不然攻擊者能用回應差異窮舉出活動內所有暱稱，再針對確定存在的暱稱窮舉手機碼。

### D4. 候選時段改採三態表態（`available`/`if_needed`/`unavailable`），`ParticipantResponse.slots` 改為帶 through model 的 M2M

第一版決策（已作廢，原文見下方存檔）：候選時段複選，`ParticipantResponse.slots` 用 plain `ManyToManyField(Slot)`，不建 through model。

> **修訂記錄（2026-09-21）**：使用者事後明確要求改為三態表態，不是二元複選——每個候選時段都要能表達「可以」／「勉強可以」／「沒辦法」三種狀態，不是只有「選了」或「沒選」。這代表關聯本身需要額外欄位（`availability`），plain `ManyToManyField` 不夠用，改成帶 `through="ParticipantResponseSlotAvailability"` 的 M2M。已透過 AskUserQuestion 確認四個關鍵點：① enum 字串值採 `available`/`if_needed`/`unavailable`（英文 snake_case，跟既有 `mode` 欄位命名風格一致）；② request body 用物件陣列 `[{slotId, availability}, ...]`（不用 `{slotId: availability}` 對照表）；③ 每次送出（初次投票／更新投票）都必須涵蓋該活動**全部**候選時段、每個恰好一次，不可省略也不可重複——省略不視為預設某個狀態，直接拒絕整筆請求；④ `GET /api/events/{id}` 的彙整頁（D8）一併顯示三態，不是只有「有選/沒選」。
>
> 欄位命名同步從 `selectedSlotIds`（純 id 陣列）改為 `slotAvailabilities`（帶狀態的物件陣列）——舊名稱在新結構下會誤導（不再只是「有被選中的 id 清單」），沿用「陣列元素是完整表態」的新語意重新命名，屬於這次結構改動的自然結果，非獨立決策。
>
> **原第一版理由（存檔）**：複選，且當時判斷沒有「每個關聯本身還要帶額外欄位」的需求（不像 `Slot` 相對 `Event` 需要 `date`/`time`/`label`），plain `ManyToManyField` 讓 Django 自動建中介表即可。這個判斷在三態需求出現後不再成立。

### D4a. `ParticipantResponseSlotAvailability`：新增 through model，`unique_together (response, slot)` 保證同一參與者對同一時段只有一筆表態

`response`／`slot` 兩個 FK 皆 `CASCADE`（參與者投票或候選時段被刪除時，表態紀錄一併清除，不留孤兒資料）。`availability` 為 `CharField(choices=...)`，三態存字串值（`available`/`if_needed`/`unavailable`），不用獨立的 `TextChoices` 拆成三個 boolean 欄位——三態本質上是單一維度的列舉，一個欄位比三個互斥 boolean 欄位更不容易出現「三個欄位同時為 true」這種不合法狀態。

寫入策略：初次投票（`create()`）用 `bulk_create` 一次寫入全部表態列；更新投票（`PATCH`）用「先刪除該筆投票既有的全部表態列、再 `bulk_create` 新的一批」，不用逐筆 `update_or_create`——請求本身要求每次都是全量覆蓋（見 D4 修訂記錄③），先刪後建邏輯簡單、不用比對哪些筆要新增/更新/刪除，且整段包在 `transaction.atomic()` 內，失敗會整個回滾。

### D5. 暱稱唯一性：trim 後精確比對（大小寫敏感），DB 層 `unique_together (event, nickname)` 保證並發安全

`nickname` 欄位儲存時已經 trim 過（serializer `validate_nickname` 內處理），DB 唯一約束比對到的就是 trim 後的值，不需要額外的 normalized 欄位。兩個請求同時搶同一個暱稱時，DB 唯一約束是最終防線——`IntegrityError` 捕獲後回 400 `NICKNAME_TAKEN`。

`ParticipantResponse.id` 也改用短 id（見 D9），同一個 `create()` 呼叫因此有兩種可能導致 `IntegrityError` 的獨立原因（暱稱重複、id 碰撞），兩者處理方式相反（前者不該重試、後者該重試），不能用同一種「捕獲就重試」邏輯處理，見 D9 的disambiguation 做法。

### D9. `ParticipantResponse.id` 採 8 碼短 id（同 `Event.id` 產生器），`IntegrityError` 捕獲後靠查詢區分成因再決定重試或拒絕

已與使用者確認：`responseId` 比照 `Event.id` 用短 id，不用 UUID——短、URL 友善、跟活動識別碼風格一致。直接重用既有 `apps/events/ids.generate_short_id`，`ParticipantResponseCreateSerializer.create()` 比照 `EventCreateSerializer.create()` 的碰撞重試迴圈（`transaction.atomic()` 包住 `create()` + `slots.set()`，捕獲 `IntegrityError` 重試數次）。

差異在於：`EventCreateSerializer.create()` 只有一個唯一約束（`Event.id`）可能觸發 `IntegrityError`，捕獲就直接重試沒有歧義；這裡 `ParticipantResponse` 同時有 `id`（短 id 碰撞，機率極低）與 `unique_together (event, nickname)`（暱稱重複，機率不低，且不該重試——重試只會換一個新 id，不會讓重複的暱稱變得不重複，只會一路重試到次數用完後把 `IntegrityError` 原樣往外拋，變成使用者看到的是 500 而不是預期的 400 `NICKNAME_TAKEN`）兩個獨立來源都可能觸發同一種例外。做法：`except IntegrityError` 內先查詢 `ParticipantResponse.objects.filter(event=event, nickname=nickname).exists()`——`transaction.atomic()` 已確保失敗的 insert 完全回滾，此時查到存在即代表暱稱衝突是由「別人已提交的資料」造成（不是我方這次失敗的 insert 殘留），直接回 400 `NICKNAME_TAKEN`，不重試；查無則視為 id 碰撞，進入既有重試邏輯。

### D6. 三支參與者端點共用的前置條件檢查順序：資源存在 → 連結未失效 → 活動狀態

`_get_event_or_404` → 連結失效檢查（`compute_display_status` 為 `link_expired` 時 410 `LINK_EXPIRED`）→ 活動狀態檢查（`status != active` 時 409 `EVENT_NOT_ACTIVE`；`status == active` 但 `now >= response_deadline` 時 409 `VOTING_CLOSED`，這是本次新增的 code，區分「活動被取消／定案」與「單純投票已截止但活動還在」兩種語意不同的 409）。三支端點（建立投票、核對身分、修改投票）皆套用同一組檢查，抽成共用函式，避免三處各寫一次容易不一致。

### D7. 不加樂觀鎖，修改投票採 last-write-wins

情境是同一參與者自己用同一組暱稱＋手機末三碼登入修改，不是多人協作同一筆投票，衝突機率低、後果輕（頂多蓋掉自己剛剛另一個分頁的修改）。比照 `add-event-patch` D5 的既有先例。

### D8. `GET /api/events/{id}` 的 `responses` 回傳每筆投票的 `nickname`＋`slotAvailabilities`（三態），不含 `phoneLastThree`/`email`

已與使用者確認唯讀彙整頁要列出「誰投了什麼」，不是純數字統計。`phoneLastThree`（即使是雜湊）與 `email` 屬於參與者的聯絡資訊，不對外（含其他參與者）公開，只在後端驗證流程內部使用。前端若要算「每個時段幾票」，可自行從這份列表 reduce，不需要後端另外算一份 `voteCount`。

> **修訂記錄（2026-09-21）**：`selectedSlotIds`（純 id 陣列）改為 `slotAvailabilities`（`[{slotId, availability}, ...]`），跟隨 D4 的三態表態改動——彙整頁一併顯示每位參與者對每個時段的明確狀態（`available`/`if_needed`/`unavailable`），不是只有「有選/沒選」的二元資訊，已與使用者確認這是本次三態需求的必要延伸，不留到未來 change。

### D10. `VOTING_CLOSED` 統一回 409（曾短暫讓 `POST .../responses` 單獨回 400，已改回）

commit 後使用者追加要求：三支參與者端點共用的 `_check_participation_preconditions` 原本 `VOTING_CLOSED` 統一回 409（跟 `EVENT_NOT_ACTIVE`、既有 `PATCH /api/events/{id}` 的 409 慣例一致）。第一版曾依當時的指示，讓 `POST .../responses`（建立投票）這一支單獨改回 400、`verify`／`PATCH` 維持 409（共用函式新增 `voting_closed_status_code` 參數）。

> **修訂記錄（2026-09-21）**：使用者確認不要這個不對稱，三支端點的 `VOTING_CLOSED` 統一改回 409——跟專案既有「狀態衝突用 409、欄位驗證用 400」的錯誤代碼慣例一致（見 `add-error-code-table`）。移除 `voting_closed_status_code` 參數，`_check_participation_preconditions` 固定回 409。

### D11. 參與者暱稱不可與主揪 `hostNickname` 相同（`NICKNAME_CONFLICTS_WITH_HOST`）

commit 後使用者追加要求：初次投票時，暱稱除了不可與既有參與者暱稱重複（`NICKNAME_TAKEN`，D5），也不可與該活動主揪的 `hostNickname` 相同——避免參與者列表出現一個看起來像主揪本人、但其實是別人冒用的暱稱。比對規則沿用既有暱稱比對慣例（D5）：trim 後精確比對、大小寫敏感。`Event.host_nickname` 是靜態欄位（同一活動內不會被併發修改成跟某個參與者暱稱衝突的值——`EventPatchSerializer` 改 `hostNickname` 時不會回頭檢查既有參與者），所以這個檢查純粹是 serializer 層的驗證（`validate_nickname` 內比對，透過 `context={"event": event}` 拿到 `event.host_nickname`），不需要 DB 唯一約束或 `IntegrityError` 這類並發防護——這點跟 D5/D9（暱稱互相比對，需要 DB 約束防 race）不同。

### D12. `ParticipantResponse.comment`：選填留言，本次只接受並儲存，不做顯示 API

commit 後使用者追加要求：初次投票時可附上一段選填留言，供未來另一支「顯示所有人留言」的 API 使用——**本次 scope 明確只到「接受並儲存」**，不做顯示/彙整 API（那是未來 change 的範圍）。長度上限比照 `Event.final_note`（200 字），沒有格式驗證（純自由文字）。刻意讓空字串（`""`）視為「沒有留言」而非驗證錯誤（`allow_blank=True`，正規化成 `None` 存入 DB）——這跟 `email` 欄位空字串視為無效（`PARTICIPANT_EMAIL_INVALID`）不同，因為 email 有「格式對不對」的概念、comment 沒有。`GET /api/events/{id}` 的 `responses` 欄位（D8）刻意不含 `comment`——D8 的彙整頁範圍是「誰投了什麼時段」，留言顯示是明確排除在本次 scope 外的獨立功能。

> **修訂記錄（2026-09-21）**：使用者確認要反轉「不做顯示」的決定，`responses` 彙整正式加入 `comment` 欄位（未留言為 `null`）。理由：使用者本來就是為了讓前端能「立即渲染該活動的投票情況跟留言情況」才提出這次追加，見 D16。

### D13. `PATCH .../responses/{responseId}` 的一次性 token 消費，compare-and-swap 需一併重新核對到期時間

Codex 二次審查抓到：`patch()` 開頭的早期檢查（token 是否存在／已用過／過期／對得上 `responseId` 與活動）用的是請求一開始取得的 `now`，但真正保證一次性消費的 compare-and-swap（`UPDATE ... WHERE used_at IS NULL`）發生在共用前置條件檢查、slot 驗證之後——這中間有真實的時間間隔。原本的 CAS 只檢查 `used_at IS NULL`，沒有重新檢查 `expires_at`：若 token 剛好在早期檢查通過之後、CAS 執行之前的空檔到期，仍會成功消費、修改投票，跟 spec「憑有效、未過期 token」的要求有落差（這跟 D9/token 的一次性消費 race 是同一類「用舊資料判斷、忘了在真正寫入的瞬間重新核對」的錯誤，只是這次是「到期時間」而非「是否已使用」）。

修法：CAS 的 `UPDATE` 加上 `expires_at__gt=<CAS 執行當下重新取得的 now>` 條件，`claimed_at`（CAS 用的當下時間）與 `used_at` 寫入值用同一個變數，不沿用早期檢查的舊 `now`。已用真實時間流逝驗證（在 `_check_participation_preconditions` 呼叫點人為注入延遲，讓效期極短的 token 確實在早期檢查與 CAS 之間到期），修正前測試會失敗（token 仍被成功消費）、修正後通過。

### D14. `selectedSlotIds` 元素非合法 UUID 字串時，補上 `SLOT_ID_INVALID` 語意化 code

用實際請求測試畸形 request body 時發現：`selectedSlotIds` 送入非 UUID 字串的元素（例如舊版三態 `{"id":..., "availability":...}` 物件格式），DRF `UUIDField` 產生的原始 `.code` 是 `"invalid"`，但 `FIELD_CODE_OVERRIDES` 只對照了 `selectedSlotIds` 的 `required`/`empty`，漏了 `invalid`，導致回應 `code: "invalid"`（未語意化，前端拿不到穩定字串）。已與使用者確認補上 `("selectedSlotIds", "invalid"): "SLOT_ID_INVALID"`——`ParticipantResponseCreateSerializer`/`ParticipantResponsePatchSerializer` 的 `selectedSlotIds` 欄位名相同，同一張表對照即可涵蓋兩支端點，不需分別處理。

### D15. Squash migration 後，已跑過舊 migration 的本地 dev DB 需手動修復（code-review 發現）

三態改版把 task 1-6 已 commit 的 4 個 migration（0003-0006）刪除重建成單一乾淨 0003（見 D4a）。code-review 抓到：Django migration 的套用記錄（`django_migrations` 表）只認檔名，不認內容。本地開發用的 Postgres 在改版前已經跑過舊版 `0003_participantresponse`／`0004_participantresponseaccesstoken`，`migrate` 因此不會重跑新內容的 0003，導致實際 schema 停留在舊版（缺 `ParticipantResponseSlotAvailability` through table、缺 `comment` 欄位），且留下孤兒表 `events_participantresponse_slots`（舊版 plain M2M 自動產生的中介表）。

已實測確認本地 dev DB 確實中招（`\dt events_*` 只看到舊表、無 through table）。修法：手動 `DROP TABLE` 孤兒表與內容不符的舊表，並從 `django_migrations` 刪除對應的 `0003_participantresponse`／`0004_participantresponseaccesstoken` 記錄，重跑 `migrate events` 讓新版 migration 真正套用；修復後重新跑過完整測試套件（161 個測試全綠，`ruff`/`manage.py check`/`makemigrations --check` 皆乾淨）確認無殘留影響。pytest 用的 test DB 每次從 migration 檔案全新建立，不受影響，只有已存在的持久化資料庫（本地 dev）會中招。因為此分支尚未合併到 main/develop、也沒有其他協作者已 pull 這個 squash 前的版本，影響範圍僅限這台機器的本地 dev DB，不需要額外的遷移腳本或文件通知其他人。

### D16. `POST .../responses`／`PATCH .../responses/{responseId}` 回應改回傳完整活動內容，`GET` 的 `responses` 彙整正式加入 `comment`

commit 後使用者追加要求：兩支端點原本只回傳極簡的 `{"id": ...}`（create）／`{"id", "slotAvailabilities"}`（patch），使用者指出前端需要「立即渲染」該活動最新的投票與留言情況，不該再逼前端多打一次 `GET /api/events/{id}`。涉及回應格式這種對外契約的改動，先走 `grill-me` 確認三個問題：

1. **回應要多完整**：確認為直接回傳跟 `GET /api/events/{id}` 完全一樣的 `EventDetailSerializer` 輸出（含活動基本欄位＋完整 `responses` 彙整），不是只回傳 `responses` 陣列本身。
2. **comment 要不要正式顯示**：確認要，等於反轉 D12「本次不做顯示」的決定（見 D12 修訂記錄）。
3. **verify 端點要不要一併改**：確認不改——`verify` 語意是核對身分＋核發 token，還沒有真正的資料異動，維持現狀的 `accessToken`/`nickname`/`email`/`slotAvailabilities` 已足夠支援前端帶入編輯表單，不需要整包活動資料。

實作：`ParticipantResponseCreateView.post()`／`ParticipantResponseDetailView.patch()` 寫入完成後，重新用 `_event_with_responses_queryset()`（既有的 `prefetch_related("responses__slot_availabilities")` queryset，避免 N+1）查一次 `event`，序列化成 `EventDetailSerializer` 回傳，跟 `EventDetailView.get()`/`.patch()` 的既有寫法一致。`EventDetailSerializer.get_responses()` 加上 `comment` 欄位（D8/D12 同步修訂）。

### D17. 新增頂層 `slotSummary`：每個候選時段的三態票數統計

commit 後使用者追加要求：前端需要「不同時段的票數」讓使用者一眼看出哪個時段比較多人可以，之後才會再做「誰投了哪個時段」的細節（那個既有的 `responses[].slotAvailabilities` 已經涵蓋，不需要另外設計）。事涉新增對外回應欄位，先走 grill-me 確認三個問題：

1. **放哪裡**：確認新增獨立的頂層陣列欄位 `slotSummary`（跟 `slots` 對稱，每個元素對應一個候選時段），不是塞進 `slots[]` 每個元素裡——不更動既有 `SlotSerializer` 的輸出格式。
2. **算什麼**：確認只拆三態原始票數（`available`/`if_needed`/`unavailable` 三個整數），不額外算「可出席合計」之類的衍生值——前端需要的話自己加，避免後端替前端做決定。
3. **key 命名**：確認三個計數欄位直接沿用既有 `availability` enum 值本身當 key（`available`/`if_needed`/`unavailable`，snake_case），不是另外設計一套 `availableCount` 這種 camelCase 命名——避免同一個概念在 API 裡有兩套字串。

每個元素格式：`{"slotId": <候選時段識別碼>, "available": <int>, "if_needed": <int>, "unavailable": <int>}`，陣列順序跟 `slots` 一致（皆為 `event.slots.all()` 的順序）。`GET /api/events/{id}`、`POST .../responses`、`PATCH .../responses/{responseId}` 三個回傳完整活動內容的端點皆包含此欄位（`EventDetailSerializer` 統一序列化，見 D16）。

實作：`EventDetailSerializer.get_slotSummary()` 用已經 `prefetch_related("slots", "responses__slot_availabilities")`（`_event_with_responses_queryset()`，本次一併把 `slots` 也加進 prefetch，避免這個新欄位跟既有 `slots` 欄位各自對 `event.slots.all()` 下一次查詢）過的資料在 Python 端累加計數，不對 DB 另下 `COUNT`/`GROUP BY` query。

### D18. `POST .../verify` 回應補上該筆投票的 `id`（前端組 PATCH URL 需要）

使用者提問時發現的真實缺口，非設計取捨、直接修：`PATCH /api/events/{id}/responses/{responseId}` 的 URL 需要 `responseId`。首次投票（`POST .../responses`）成功後，回應是完整活動內容（D16），前端可以從 `responses[]` 用自己剛送出的 `nickname` 找到那筆的 `id`，存下來（例如 localStorage）給同一裝置未來直接用；但換裝置、或本地儲存被清除後，`verify` 端點是前端唯一能重新取得這個 `id` 的來源——而 `verify` 原本的回應只有 `accessToken`/`expiresAt`/`nickname`/`email`/`slotAvailabilities`，沒有 `id`，前端拿到 token 卻沒有東西可以拼下一次 `PATCH` 的 URL。修法：`ParticipantResponseVerifyView.post()` 回應加上 `"id": participant_response.id`。

## Risks / Trade-offs

- **[風險] 手機末三碼可被暴力窮舉冒用身分改票（D1）** → 已與使用者確認為本次刻意接受的風險，不在 scope 內處理。緩解方向留給未來 change：失敗次數鎖定、或核對 API 加 IP／裝置層級的 rate limit。
- **[風險] Access token 效期內若外流（例如瀏覽器分頁被他人接手使用），30 分鐘內可被用來修改投票** → 緩解：token 只存雜湊、只能使用一次、效期短；風險程度與「暱稱鎖定不可改」「僅能改時段（不能改聯絡資訊）」的範圍限制一致，可造成的傷害有限。
- **[風險] `unique_together (event, nickname)` 意味著同一活動下兩個不同的人剛好想用同一個暱稱，後來者會被擋** → 這是規格本身的既定行為（「暱稱不可與既有暱稱重複」），不是本次技術限制的副作用。
- **[風險] `ParticipantResponseAccessToken` 無清除策略，資料表隨核對次數無限累積**（Codex 二次審查提出）→ 每次呼叫 `verify` 都會新增一筆 token，即使已過期或已使用也永久保留，非本次 blocker，留給未來 change：定期清除過期／已使用超過一段時間的 token；或核發新 token 時順便刪掉同一 response 的舊 token；`expires_at` 若要支援排程清理，屆時可補 index。

## Migration Plan

新增 migration：`ParticipantResponse`（FK `Event`，M2M `Slot`）、`ParticipantResponseAccessToken`（FK `ParticipantResponse`）。純新增資料表，不修改既有 `Event`/`Slot` schema，回滾只需 reverse migration，不影響既有端點。
