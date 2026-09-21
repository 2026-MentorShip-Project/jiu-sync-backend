## Why

主揪建立活動後，參與者拿到分享連結目前只能看到活動資料（`GET /api/events/{id}`），`responses` 欄位固定回傳空陣列——沒有任何管道能真的投票，或事後修改投票，唯讀彙整頁也看不到任何結果。需要讓參與者首次投票、事後修改投票，並讓 `GET /api/events/{id}` 真正回傳彙整結果。

## What Changes

- 新增 `ParticipantResponse` model（暱稱、手機末三碼雜湊、選填 Email，與 `Event` 多對一、與 `Slot` 多對多）
- 新增 `POST /api/events/{id}/responses`：初次投票，暱稱（trim 後於該活動內須唯一）＋手機末三碼（雜湊儲存）＋選填 Email＋複選候選時段
- 新增 `POST /api/events/{id}/responses/verify`：以暱稱＋手機末三碼核對身分，成功核發 30 分鐘效期的一次性 access token（供接續的 `PATCH` 使用）；核對失敗一律回同一個錯誤，不區分「暱稱不存在」與「手機碼錯誤」
- 新增 `PATCH /api/events/{id}/responses/{responseId}`：憑上述 token 修改已選候選時段；暱稱、手機末三碼、Email 皆鎖定，不可透過此端點修改
- 修改 `GET /api/events/{id}`：`responses` 欄位從寫死空陣列改為真實回傳每筆投票的暱稱與所選時段 id 列表（不含手機／Email 等聯絡資訊）
- 三支參與者端點共用前置條件檢查：活動存在、連結未失效（410 `LINK_EXPIRED`）、活動狀態為進行中且未過投票截止時間（否則依原因回 409 `EVENT_NOT_ACTIVE` 或 409 `VOTING_CLOSED`）

未涵蓋（明確排除於本次 scope）：手機末三碼暴力破解防禦（已與使用者確認，本次僅雜湊儲存，不加失敗次數限制或鎖定機制，風險見 design.md Risks）；更新投票時修改 Email 或暱稱；前端唯讀彙整頁本身的呈現（本次只交付後端 API）。

## Capabilities

### New Capabilities

（無 — 沿用既有 `events` capability，新增／修改其下的 Requirement）

### Modified Capabilities

- `events`：新增三條 Requirement（參與者初次投票、參與者身分核對、參與者更新投票），並修改既有「查詢單一活動完整資料」Requirement 使 `responses` 欄位真實回傳彙整結果。本次 delta 建立在 `add-events-api`／`add-event-patch` 已定案但尚未 archive 的 `events` capability 內容之上（見 `openspec/changes/add-events-api/specs/events/spec.md`、`openspec/changes/add-event-patch/specs/events/spec.md`），待該兩個變更 archive 後三者會合併進同一份 `openspec/specs/events/spec.md`。

## Impact

- 新增 `apps/events/models.py`：`ParticipantResponse`（FK `Event`，M2M `Slot`）、`ParticipantResponseAccessToken`，含 migration
- 新增 `apps/events/serializers.py`：`ParticipantResponseCreateSerializer`、`ParticipantResponseVerifySerializer`、`ParticipantResponsePatchSerializer`；修改 `EventDetailSerializer.get_responses()`
- 新增 `apps/events/views.py`：`ParticipantResponseCreateView`、`ParticipantResponseVerifyView`、`ParticipantResponseDetailView`；新增三支端點共用的前置條件檢查函式
- 修改 `apps/events/urls.py`：新增三條路由
- 修改 `config/exceptions.py`：`FIELD_CODE_OVERRIDES` 補上本次新增欄位的 code
- `responseId` 比照 `Event.id` 採 8 碼 base62 短 id（重用既有 `apps/events/ids.generate_short_id`），非 UUID；碰撞重試邏輯需與「暱稱重複」的 `IntegrityError` 分開判斷，見 design.md D9
- 已同步更新前後端對焦用的 Swagger artifact（`https://claude.ai/artifact/FgVxgLaS7L8Pf2MNEz92Vf`，由 prototype-7f session 維護）：新增本次三支端點、`ParticipantResponse`/`ApiError` schema 改版、`responseId` 短 id 格式說明；並標記舊版 `POST /api/responses/mine`（Email＋密碼查詢）因密碼欄位拿掉而設計基礎不成立，待後續 change 處理
- **commit 後追加**（design.md D11/D12）：`ParticipantResponse` 新增選填 `comment` 欄位（本次僅接受並儲存，顯示 API 留待未來 change）；暱稱新增不可與主揪 `hostNickname` 相同的規則（`NICKNAME_CONFLICTS_WITH_HOST`）
- **Codex 二次審查**（design.md D13）：`PATCH .../responses/{responseId}` 的一次性 token compare-and-swap 補上到期時間重新核對，修掉早期檢查與真正消費之間的極短空檔可能讓過期 token 仍成功消費的落差；`ParticipantResponseAccessToken` 無清除策略記入 Risks，非本次 blocker
- **實測畸形 request body 發現**（design.md D14）：`selectedSlotIds` 元素非 UUID 格式時補上語意化 code `SLOT_ID_INVALID`（原本回傳未對照的 DRF 原始碼 `"invalid"`）
- **commit 後追加，候選時段改三態**（design.md D4 2026-09-21 修訂／D4a）：候選時段從二元複選（`selectedSlotIds` id 陣列）改為 `available`/`if_needed`/`unavailable` 三態表態（`slotAvailabilities: [{slotId, availability}, ...]`），須恰好涵蓋該活動全部候選時段各一次。`ParticipantResponse.slots` 改為帶 `ParticipantResponseSlotAvailability` through model 的 M2M；三支序列化器與 `GET` 的 `responses` 欄位皆改用新格式；新增 `SLOT_AVAILABILITIES_REQUIRED`/`SLOT_ID_INVALID`/`AVAILABILITY_INVALID`/`SLOT_AVAILABILITY_INCOMPLETE` 錯誤代碼。因不支援的 M2M schema 變更，task 1-6 的 4 個舊 migration 已刪除重建成單一乾淨 migration（皆未合併到 main/develop，僅存在此未合併分支）。過程中額外修正一個既有的 `config/exceptions.py::_build_errors` 巢狀陣列錯誤形狀誤判（自 `add-events-api` 以來 `slots[].date` 等巢狀 code 對照從未真正生效過）與一個測試 fixture 的 `.first()` 排序 flaky 問題，皆已補回歸測試，詳見 tasks.md 7.2 與同日 incident-log
- **code-review 發現並修復**（design.md D15）：squash migration 後本地 dev DB 因舊 migration 記錄殘留而未真正套用新 schema（缺 through table、留孤兒表），已手動修復並重跑完整驗證套件確認無殘留影響
- ⚠️ **待辦**：Swagger artifact（`https://claude.ai/artifact/FgVxgLaS7L8Pf2MNEz92Vf`）尚未同步這次三態改動（仍是 task 1-6 commit 時同步的二元複選版本），需要另一輪更新
