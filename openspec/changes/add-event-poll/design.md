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

### D5. 需要 `eventUpdatedAt` 才能偵測「活動本身」的變化(code review 補修)

Codex code-review 抓到一個真實漏洞:`responseCount`/`commentCount`/`latestResponseAt`/`latestCommentAt` 只能反映投票跟留言的變化,偵測不到「活動本身」的變化——例如主揪在同一個輪詢週期內 `reopen` 後又重新 `finalize`、改選另一個 `finalSlotId`/`finalNote`,或單純改了 `title`/`location` 等欄位。若這段期間投票數、留言數都沒變,`/poll` 回應會跟上一次完全相同,前端誤判「沒有變化」而不重新拉取完整活動內容,畫面停在舊的定案時段。

根因:`Event.updated_at`(`auto_now=True`)本來就存在,但 `EventFinalizeView`/`EventCancelView`/`EventReopenView` 都是 compare-and-swap 模式的 `QuerySet.update()`,不是 `Model.save()`——`auto_now` 只在 `.save()`/`.create()` 生效,對 `.update()` 不生效,這三支从來没真的蓋过这个欄位(跟 D2 的 `ParticipantResponse.updated_at` 同一種坑)。`EventDetailView.patch()` 走 `serializer.save()`,原生行為本來就正確,不受影響。

修法:比照 D2 同款手法,在 `EventFinalizeView`/`EventCancelView`/`EventReopenView` 既有的 `.update()` 呼叫裡各自補上 `updated_at=claimed_at`(沿用同一個已經算好的時間戳記),`/poll` response 新增 `eventUpdatedAt` 欄位。

取捨考量(比較過 codex 建議的「revision 遞增版本號」方案後決定不採用):
- `Event.updated_at` 已存在,不需要新欄位、不需要新 migration;revision 需要新增欄位跟 migration。
- 缺口只有 3 個既有 `.update()` 呼叫點各補一個參數;revision 除了這 3 處還要在 `EventDetailView.patch()` 額外處理 `F("updated_at")+1` 式的遞增邏輯(該處走 `.save()`,不能沿用同一招)。
- revision 主要是為了避免「同一次 request 內狀態被改回原值,比對值看不出差異」——但這裡要偵測的是「有沒有異動」,不是「比對值本身是否相同」,而人為操作(reopen 再 finalize)間隔是使用者手動操作的秒級時間,`updated_at` 不會撞在同一個 microsecond,不需要 revision 額外提供的單調遞增保證。

## Risks / Trade-offs

- **[風險] 高頻端點(10 秒一次、每個開著頁面的使用者各自輪詢)增加資料庫負載** → 目前查詢只有兩個 aggregate(`Count`+`Max`),沒有 join 明細資料,成本遠低於完整 `GET /api/events/{id}`(含 `responses`/`slotSummary` 的完整序列化)。若未來真的成為瓶頸,屬於獨立的效能優化 change(例如加 cache、改長輪詢間隔),非本次 scope。
- **[風險] 前端輪詢間隔與伺服器端沒有任何協調機制** → 10 秒是前端自訂的固定間隔,伺服器端完全被動回應,沒有 rate limit。這次沒有特別處理,若未來濫用成為問題,屬於獨立的 rate limiting change。

## Migration Plan

不需要新 migration——`ParticipantResponse.updated_at` 其實從最初的 `0003_participantresponse` migration 就已經存在(跟 `Event.finalized_at` 等欄位同款的 schema-ahead 慣例),只是從未被任何程式碼寫入過。這次只需要讓 `PATCH` 端點真的去更新它。
