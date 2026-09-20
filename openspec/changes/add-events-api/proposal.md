## Why

揪甘心後端目前 `apps/events` 是空殼(model/view/serializer/urls 皆未實作),主揪無法建立活動、也拿不到活動連結分享給參與者。前端已有完整 API 契約(2026-09-15/16 版,已與使用者確認)在等後端實作,這是產品從「只有登入」走到「能真正揪團」的第一步。

## What Changes

- 新增 `Event` model:含 `owner`(FK User)、`title`、`host_nickname`、`host_email`(nullable)、`mode`、`response_deadline`、`location`、`description`、`status`(active/finalized/cancelled)、`finalized_at`/`cancelled_at`(nullable,供未來 displayStatus 計算用)、`final_slot_id`/`final_note`(nullable,供未來定案功能用)
- 新增 `Slot` model(獨立於 Event 的 model,非 JSONField):`event` FK、`date`、`time`(nullable)、`label`(nullable)
- 新增 `POST /api/events`:需登入,建立活動與其候選時段,回傳 `{id, shareUrl}`
- 新增 `GET /api/events/{id}`:公開(不需登入),回傳活動完整資料,含後端計算的 `displayStatus`(六態衍生欄位,不落地資料庫)與 `isOwner`
- 新增 `GET /api/events?owner=me`:需登入,回傳目前使用者擁有的活動清單(精簡格式,含已取消活動)

未涵蓋(明確排除於本次 scope):`PATCH /api/events/{id}`(編輯基本資訊)、finalize/cancel 相關端點、`GET /api/events/{id}/live`(輪詢)、參與者投票(`ParticipantResponse` model 與 submitResponse 端點)、AI 餐廳推薦。

## Capabilities

### New Capabilities
- `events`: 主揪建立活動、取得單一活動完整資料、取得自己擁有的活動清單

### Modified Capabilities

(無;`user-auth`、`api-error-format` 兩個既有 capability 的需求本身不變,本次只是延用其既有機制)

## Impact

- 新增 `apps/events/models.py` 的 `Event`、`Slot` model 與對應 migration
- 新增 `apps/events/serializers.py`:`EventDetailSerializer`、`EventSummarySerializer`
- 新增 `apps/events/views.py`:對應三支 endpoint 的 view
- 修改 `apps/events/urls.py`:掛上述三支路由
- 新增 `settings.FRONTEND_BASE_URL` 環境變數(`config/settings/base.py` 讀取,`dev.py`/`prod.py` 各自預設值),供組 `shareUrl` 用
- 資料庫新增兩張表(`Event`、`Slot`),需要一支 migration
