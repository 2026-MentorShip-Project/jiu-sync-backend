## Why

前端每 10 秒要判斷「這個活動有沒有新投票、狀態有沒有變(定案/取消/重開)、有沒有新留言」,才能決定要不要重新拉最新資料。舊版前端規格曾有 `GET /events/{id}/live` 打算做這件事,但從未在真實後端實作過,且先前一輪跟使用者確認後拿掉了輪詢設計(見 Swagger 文件 2026-09-24 差異列表)。這次使用者明確要求把輪詢加回來,但改成獨立、輕量的設計,不是照舊版恢復。

## What Changes

- 新增 `GET /api/events/{id}/poll`:回傳輕量的「有沒有變化」訊號,不含 `responses`/`slotSummary`/`comments` 明細內容——`{status, displayStatus, responseCount, latestResponseAt, commentCount, latestCommentAt}`。前端每 10 秒打這支,跟上一次拿到的值比對,有差異才去打 `GET /api/events/{id}`(取得完整投票/狀態)跟 `GET /api/events/{id}/comments`(取得留言內容),沒有差異就什麼都不做
- 公開端點(不需登入,跟 `GET /api/events/{id}` 同一批對象——任何有連結的人),同樣受連結失效規則約束:活動 `displayStatus` 算出 `link_expired` 時回 410,前端該停止輪詢
- `PATCH /api/events/{id}/responses/{responseId}`(參與者改票)成功時額外把 `ParticipantResponse.updated_at` 打上時間戳記——原本這個端點只會動 `ParticipantResponseSlotAvailability` 子表,完全不觸碰 `ParticipantResponse` 本身任何欄位,單靠既有的 `created_at` 只能偵測到「新投票」,偵不到「已有投票被修改」,`latestResponseAt` 需要這個欄位才能真正反映最後一次异動(不論新投或改票)。`updated_at` 欄位本身其實從最初的 migration 就已經存在(schema-ahead,從未被寫入過),不需要新 migration

未涵蓋(明確排除):不做伺服器推播(SSE/WebSocket)——維持前端輪詢模式,跟使用者的原始需求一致;不在 `/poll` 回應裡帶任何個別參與者/留言的明細內容(暱稱、留言文字等),只帶數量與時間戳記,維持輕量。

## Capabilities

### New Capabilities

(無)

### Modified Capabilities

- `events`:新增 `GET /api/events/{id}/poll` 端點與其輕量回應規則;`PATCH /api/events/{id}/responses/{responseId}` 成功時額外更新 `ParticipantResponse.updated_at`

## Impact

- 修改 `apps/events/views.py`:`ParticipantResponseDetailView.patch()` 成功時多一次輕量 `.update(updated_at=...)`;新增 `EventPollView`
- 修改 `apps/events/urls.py`:新增 `<shortid:id>/poll/` 路由
