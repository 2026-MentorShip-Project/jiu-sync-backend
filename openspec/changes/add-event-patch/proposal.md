## Why

`add-events-api` 讓主揪能建立活動,但建立後發現標題打錯字、時間訂錯、想換聯絡信箱都無法修改,只能整團重開重新分享連結。主揪需要一支能編輯自己活動基本資訊的端點。

## What Changes

- 新增 `PATCH /api/events/{id}`:已登入主揪可編輯自己活動的六個基本欄位(`title`/`description`/`location`/`hostNickname`/`hostEmail`/`responseDeadline`),partial update,成功回傳完整活動資料(格式同 `GET /api/events/{id}`)
- 新增擁有者檢查:非本人編輯回 403
- 新增活動狀態檢查:僅 `status=active` 的活動可編輯,`finalized`/`cancelled` 回 400(目前無法透過任何既有端點產生這兩種狀態的資料,此檢查是為未來 finalize/cancel change 預先立好邊界)

未涵蓋(明確排除於本次 scope):`mode`、`slots`(候選時段)不可編輯;finalize/cancel 端點本身;`/live` 輪詢;參與者投票;併發衝突偵測(採 last-write-wins,不加樂觀鎖)。

## Capabilities

### New Capabilities

(無)

### Modified Capabilities

- `events`:新增一條 Requirement「主揪編輯活動基本資訊」。此 capability 尚未歸檔至 `openspec/specs/events/spec.md`(來源變更 `add-events-api` 仍在 PR review 階段、尚未 merge/archive),本次 delta 建立在 `openspec/changes/add-events-api/specs/events/spec.md` 已定案的內容之上,待該變更 archive 後兩者會自然合併進同一份 `openspec/specs/events/spec.md`。

## Impact

- 修改 `apps/events/views.py`:`EventDetailView` 新增 `patch()` 方法,依 HTTP method 分派不同的認證/權限規則(`GET` 維持寬鬆認證,`PATCH` 要求嚴格 `IsAuthenticated` + 擁有者檢查)
- 新增 `apps/events/serializers.py` 的 `EventPatchSerializer`(重用既有 `_weighted_length` CJK 加權長度驗證函式)
- 不修改 `apps/events/models.py`、不新增 migration(六個可改欄位皆已存在於 `Event` model)
