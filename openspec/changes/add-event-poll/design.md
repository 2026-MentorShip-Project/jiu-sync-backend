## Context

使用者的原始想法有兩個選項猶豫:「這一支 API 直接回覆最新情況讓前端呈現」vs「只回一個訊號,前端自己判斷要不要再拉」,並自己點出前者「似乎太重」。跟使用者確認過(grill-me),採輕量訊號設計。

這個 change 依賴 `ParticipantResponse.deleted_at`(取消活動時軟刪除既有投票)——這個欄位是 `add-event-lifecycle` change 才新增的,`develop` 當下還沒有,所以這個 change 的 base 選在 `feature/add-event-lifecycle`(已包含 lifecycle+reopen+comments,即將透過 PR#18 併回 develop 的分支),不是直接從 `develop` 分支出去。

## Decisions

### D1. 回應輕量訊號,不回完整活動內容

跟使用者確認:`/poll` 只回 `{status, displayStatus, responseCount, latestResponseAt, commentCount, latestCommentAt}`,不含 `responses`/`slotSummary`/`comments` 陣列本身。理由:
- 每 10 秒一次的高頻端點,若每次都回完整投票明細跟全部留言內容,參與者多、留言多時頻寬浪費明顯(使用者自己的顧慮)
- 前端比對訊號有沒有變化,有變才另外打 `GET /api/events/{id}`/`GET /api/events/{id}/comments` 取得完整內容——這兩支端點已經存在,不需要重新設計回應格式
- 取捨:多一次額外的網路往返(偵測到變化後才拉完整資料),但發生頻率遠低於「什麼都沒變」的情況(多數輪詢週期應該是沒有變化的),整體流量遠低於每次都回完整內容

### D2. 需要 `ParticipantResponse.updated_at` 才能偵測到「改票」

原本設想只用 `MAX(created_at)` 當作 `latestResponseAt`,但 `PATCH /api/events/{id}/responses/{responseId}`(參與者改票)目前完全不觸碰 `ParticipantResponse` 這個 model 本身的任何欄位——只刪除重建 `ParticipantResponseSlotAvailability` 子表(見 `views.py` 的實作,design.md `add-participant-responses` D4a)。只用 `created_at` 的話,同一位參與者改了候選時段選擇,`/poll` 完全偵測不到這個變化。

跟使用者確認過(grill-me):`ParticipantResponseDetailView.patch()` 的 compare-and-swap 成功之後,在同一個 `transaction.atomic()` 裡多一次輕量 `ParticipantResponse.objects.filter(pk=...).update(updated_at=claimed_at)`(沿用同一個已經算好的時間戳記,不重新呼叫 `timezone.now()`)。

`auto_now=True` 本身在 `Model.save()`/`.create()` 時就會自動蓋章,新增投票(`ParticipantResponseCreateSerializer.create()` 用 `ParticipantResponse.objects.create(...)`)不需要額外程式碼,原生行為就會正確蓋上初始值——只有 `PATCH` 這條路徑因為改用 bulk `.update()`/子表重建、完全不碰主表,才需要手動補這一行。

`latestResponseAt` 的聚合改成 `MAX(updated_at)`(不是 `MAX(created_at)`),同時涵蓋「新投」跟「改票」兩種情境。

### D3. 端點公開、無需登入,受連結失效規則約束

跟 `GET /api/events/{id}` 同一批對象(任何有連結的人,不限主揪),用 `AllowAny` + 空 `authentication_classes`(比照 `CommentListCreateView`,不需要 `isOwner` 判斷,比 `GET /api/events/{id}` 用的 `OptionalJWTAuthentication` 更單純)。活動 `displayStatus` 算出 `link_expired` 時,`/poll` 也回 410 `LINK_EXPIRED`——前端輪詢邏輯收到 410 就該停止繼續打,跟連結失效時 `GET /api/events/{id}` 的既有行為一致,不需要前端另外判斷「這支端點的失效規則跟那支不一樣」。

### D4. `responseCount`/`commentCount` 沿用既有的軟刪除排除慣例

`responseCount` 只算 `deleted_at__isnull=True` 的 `ParticipantResponse`(排除 `cancel` 軟刪除的舊投票),`commentCount` 只算 `deleted_at__isnull=True` 的 `Comment`(排除owner 軟刪除的留言)——跟 `EventSummarySerializer.get_responseCount()`、`CommentListCreateView.get()` 的既有過濾邏輯一致,不引入新的計算規則。

## Risks / Trade-offs

- **[風險] 高頻端點(10 秒一次、每個開著頁面的使用者各自輪詢)增加資料庫負載** → 目前查詢只有兩個 aggregate(`Count`+`Max`),沒有 join 明細資料,成本遠低於完整 `GET /api/events/{id}`(含 `responses`/`slotSummary` 的完整序列化)。若未來真的成為瓶頸,屬於獨立的效能優化 change(例如加 cache、改長輪詢間隔),非本次 scope。
- **[風險] 前端輪詢間隔與伺服器端沒有任何協調機制** → 10 秒是前端自訂的固定間隔,伺服器端完全被動回應,沒有 rate limit。這次沒有特別處理,若未來濫用成為問題,屬於獨立的 rate limiting change。

## Migration Plan

不需要新 migration——`ParticipantResponse.updated_at` 其實從最初的 `0003_participantresponse` migration 就已經存在(跟 `Event.finalized_at` 等欄位同款的 schema-ahead 慣例),只是從未被任何程式碼寫入過。這次只需要讓 `PATCH` 端點真的去更新它。
