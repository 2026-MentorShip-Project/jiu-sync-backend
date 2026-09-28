## Purpose

讓活動擁有者(主揪)在活動成團後,依偏好條件取得 AI 搜尋的真實餐廳推薦,並以每人每月額度控制外部 AI 服務的使用量。

## ADDED Requirements

### Requirement: 活動擁有者可取得 AI 餐廳推薦

系統 SHALL 提供 `POST /api/events/{id}/restaurant-recommendations/`,讓已登入的活動擁有者送出偏好條件,取得 1 到 5 間 AI 推薦餐廳。成功時回傳 201,內容包含:推薦紀錄 `id`、`restaurants` 陣列、`notes`、`resolvedPreferences`、`quota`。

每間餐廳 SHALL 包含:`id`(同一次推薦內唯一、由系統產生)、`name`、`address`(兩者必為非空字串),以及 `phone`、`rating`、`reviewCount`、`openingHours`、`priceRange`、`avgPricePerPerson`(`{min, max}`)、`cuisineType`、`distanceInfo`(`{transitPoint, walkMinutes}`)、`recommendReason`、`sourceUrl`(查不到時為 `null`)。

`sourceUrl` SHALL 只來自外部 AI 服務回應中附帶的搜尋來源,不得由系統或模型自行組出網址;比對不到對應來源時為 `null`。`sourceUrl` 代表「提及此店的參考來源」,可能是彙整文章而非店家官方頁面。

#### Scenario: 成功取得推薦
- **WHEN** 活動擁有者對一個已定案、聚會日期未過的活動送出合法偏好條件,且外部 AI 服務回傳可用結果
- **THEN** 回傳 201,`restaurants` 含 1 到 5 間餐廳,每間都有非空的 `name` 與 `address` 和唯一的 `id`

#### Scenario: 外部服務回傳超過 5 間
- **WHEN** 外部 AI 服務回傳 7 間可用餐廳
- **THEN** 回應只包含前 5 間

#### Scenario: 部分結果缺少必要欄位
- **WHEN** 外部 AI 服務回傳 3 間餐廳,其中 1 間 `address` 為空
- **THEN** 該間被捨棄,回傳其餘 2 間,本次視為成功

#### Scenario: 店名只出現在來源摘要中
- **WHEN** 某間餐廳的店名沒有出現在任何來源標題中,但出現在某個來源的摘要中
- **THEN** 該間的 `sourceUrl` 為該來源的網址

#### Scenario: 來源網址比對不到
- **WHEN** 某間餐廳在外部服務附帶的搜尋來源中找不到對應項目
- **THEN** 該間的 `sourceUrl` 為 `null`,其餘欄位照常回傳

### Requirement: 僅限擁有者且活動需處於可推薦狀態

系統 SHALL 只允許活動擁有者呼叫推薦 API,且只在活動已定案、定案時段日期未過、連結未失效時執行推薦。

#### Scenario: 未登入
- **WHEN** 未帶有效 access token 呼叫推薦 API
- **THEN** 回傳 401

#### Scenario: 活動不存在
- **WHEN** 活動 id 不存在
- **THEN** 回傳 404,`code` 為 `EVENT_NOT_FOUND`

#### Scenario: 非擁有者
- **WHEN** 已登入但不是該活動擁有者的使用者呼叫
- **THEN** 回傳 403,不呼叫外部服務、不計次

#### Scenario: 活動尚未定案或已取消
- **WHEN** 活動狀態為投票中、投票截止待定案,或已取消
- **THEN** 回傳 409,`code` 為 `EVENT_NOT_FINALIZED`

#### Scenario: 聚會日期已過
- **WHEN** 活動已定案但定案時段的日期早於今天(台灣時間)
- **THEN** 回傳 409,`code` 為 `EVENT_ALREADY_PAST`

#### Scenario: 活動連結已失效
- **WHEN** 活動定案超過 7 天
- **THEN** 回傳 410,`code` 為 `LINK_EXPIRED`

### Requirement: 偏好條件全部選填,地點可由活動補齊

請求 body 的所有欄位 SHALL 為選填。系統 SHALL 以活動既有資訊補齊缺少的條件,並在回應的 `resolvedPreferences` 標示實際採用的值與地點來源(`request` 或 `event`)。

欄位與限制:
- `location`:字串,最多 100 字
- `relationship`:`同事`、`朋友`、`家人`、`社團`、`約會` 其中之一
- `budget`:`200 以下`、`200-400`、`400-600`、`600-800`、`800-1000`、`1000 以上` 其中之一(每人新台幣)
- `partySize`:`2 人`、`3-4 人`、`5-8 人`、`9 人以上（多人）`、`20 人以上（團體）` 其中之一
- `situational`:`可久坐`、`有插座`、`停車位`、`親子友善`、`無障礙` 的子集合,不可重複
- `dietary.vegetarian`:布林
- `dietary.spice`:`不吃辣`、`愛吃辣` 其中之一
- `dietary.cuisines`:字串陣列,最多 5 項,每項 1–20 字,可為任意自訂值
- `dietary.restrictions`:字串陣列,最多 5 項,每項 1–30 字,可為任意自訂值
- `customPrompt`:字串,最多 200 字

字串欄位 SHALL 去除前後空白後再驗證,去除後為空字串視同未填。清單欄位 SHALL 先去除空白項目,再檢查項目數上限與重複。`resolvedPreferences.partySize` 一律為人數規格字串或 null(不會是數字),實際人數另由 `attendeeCount`(整數)提供。

#### Scenario: 完全不填(略過,使用預設推薦)
- **WHEN** 送出空 body,且活動有設定地點
- **THEN** 以活動地點、出席人數與定案時段產生推薦,`resolvedPreferences.locationSource` 為 `event`

#### Scenario: 使用者地點優先於活動地點
- **WHEN** 送出 `location: "中山站"`,活動地點為「台北車站」
- **THEN** 以「中山站」搜尋,`resolvedPreferences.locationSource` 為 `request`

#### Scenario: 兩邊都沒有地點
- **WHEN** 請求未填 `location` 且活動沒有設定地點
- **THEN** 回傳 400,`code` 為 `LOCATION_REQUIRED`,不呼叫外部服務、不計次

#### Scenario: 未選人數時以出席人數帶入
- **WHEN** 請求未填 `partySize`
- **THEN** `resolvedPreferences.attendeeCount` 為定案時段中回覆「可以」的參與者人數,`resolvedPreferences.partySize` 為依該人數換算的人數規格字串(≤2 → `2 人`、≤4 → `3-4 人`、≤8 → `5-8 人`、≤19 → `9 人以上（多人）`、其餘 → `20 人以上（團體）`,與前端預填算法一致)

#### Scenario: 使用者有選人數
- **WHEN** 請求帶 `partySize: "5-8 人"`
- **THEN** `resolvedPreferences.partySize` 為 `5-8 人`,`attendeeCount` 仍為活動實際回覆「可以」的人數

#### Scenario: 沒有人回覆可以
- **WHEN** 請求未填 `partySize`,且定案時段沒有任何「可以」的回覆
- **THEN** `resolvedPreferences.partySize` 為 null、`attendeeCount` 為 0,推薦條件不包含人數

#### Scenario: 欄位不合法
- **WHEN** `relationship` 不在允許值內,或 `dietary.cuisines` 超過 5 項,或 `customPrompt` 超過 200 字
- **THEN** 回傳 400,`errors` 列出每個不合法欄位,不呼叫外部服務、不計次

#### Scenario: 清單含空白項目
- **WHEN** `dietary.cuisines` 送 6 項,其中 1 項是空白字串
- **THEN** 空白項目被忽略,視為 5 項,請求被接受

#### Scenario: 自訂料理
- **WHEN** 送出 `dietary.cuisines: ["泰式", "火鍋"]`
- **THEN** 請求被接受並納入推薦條件

### Requirement: 每人每月使用次數上限

系統 SHALL 限制每位使用者每個台灣時間自然月最多成功取得推薦 N 次(N 預設 20,可由設定調整)。只有成功回傳推薦(201)的請求 SHALL 計入次數;驗證失敗、狀態不符、外部服務失敗或逾時 SHALL NOT 計入。進行中的請求 SHALL 暫時佔用一次額度,直到完成或超過 5 分鐘。次數歸屬於請求建立當下的月份。

#### Scenario: 額度用完
- **WHEN** 使用者當月已成功使用 20 次後再呼叫
- **THEN** 回傳 403,`code` 為 `AI_RECOMMENDATION_QUOTA_EXCEEDED`,不呼叫外部服務

#### Scenario: 併發請求不可超用
- **WHEN** 使用者當月已用 19 次,同時對不同活動送出多個合法請求
- **THEN** 最多只有 1 個請求成功,其餘回傳 403 `AI_RECOMMENDATION_QUOTA_EXCEEDED`,當月成功次數不超過 20

#### Scenario: 失敗不計次
- **WHEN** 外部服務回傳錯誤導致推薦失敗
- **THEN** 當月已用次數不變,使用者可以立即再次呼叫

#### Scenario: 跨月重置
- **WHEN** 使用者 9 月已用完 20 次,在台灣時間 10 月 1 日 00:00 之後呼叫
- **THEN** 請求可以正常進行,10 月已用次數從 0 開始計算

#### Scenario: 重新推薦也計次
- **WHEN** 使用者對同一活動已成功取得一次推薦,之後再次送出推薦請求且成功
- **THEN** 本次視為新的一次,當月已用次數再加 1;前一次的推薦紀錄與結果保留,不被覆蓋

#### Scenario: 卡住的進行中請求不永久佔用額度
- **WHEN** 某筆進行中的請求建立超過 5 分鐘仍未完成
- **THEN** 它不再佔用額度

### Requirement: 同一活動不可同時進行多個推薦請求

使用者對同一活動已有未完成(且未超過 5 分鐘)的推薦請求時,系統 SHALL 拒絕新的請求。

#### Scenario: 連點兩下
- **WHEN** 同一使用者對同一活動近乎同時送出兩個合法請求
- **THEN** 其中一個正常處理,另一個回傳 409,`code` 為 `AI_RECOMMENDATION_IN_PROGRESS`,外部服務只被呼叫一次

### Requirement: 外部服務異常時的回應

系統 SHALL 在外部 AI 服務異常時回傳明確錯誤且不計次;錯誤 SHALL 被記錄,不可被靜默忽略。

#### Scenario: 外部服務未設定
- **WHEN** 伺服器未設定 Perplexity API key
- **THEN** 回傳 503,`code` 為 `AI_RECOMMENDATION_UNAVAILABLE`,不計次;其他 API 不受影響

#### Scenario: 外部服務逾時
- **WHEN** 外部服務在設定的逾時時間內沒有回應
- **THEN** 回傳 504,`code` 為 `AI_RECOMMENDATION_UPSTREAM_TIMEOUT`,不計次

#### Scenario: 外部服務錯誤或回應格式不符
- **WHEN** 外部服務回傳非成功狀態或回應不符合預期結構
- **THEN** 回傳 502,`code` 為 `AI_RECOMMENDATION_UPSTREAM_FAILED`,不計次

#### Scenario: 找不到任何可用餐廳
- **WHEN** 外部服務正常回應,但沒有任何一間同時具備 `name` 與 `address` 的餐廳
- **THEN** 回傳 502,`code` 為 `AI_RECOMMENDATION_UPSTREAM_FAILED`,body 額外包含 `notes`(外部服務說明找不到的原因,可能為 null),不計次

### Requirement: 推薦事件輸出結構化 log

推薦相關事件 SHALL 以每行一個 JSON 物件輸出到標準輸出,供 log 收集系統(例如 Loki)依欄位過濾與計算指標。每筆 SHALL 包含 `timestamp`(ISO 8601)、`level`、`logger`、`event`,以及該事件的欄位;數值欄位 SHALL 為 JSON 數字,不得為字串。log SHALL NOT 包含 prompt、使用者自由輸入文字、外部服務原始回應或 API key。

事件與欄位:
- `ai_rec.succeeded`(INFO):`request_id`、`user_id`、`event_id`、`latency_ms`、`restaurant_count`、`total_tokens`、`cost_usd`、`model`
- `ai_rec.failed`(WARNING;非預期例外為 ERROR):`request_id`、`user_id`、`event_id`、`error_code`、`latency_ms`、`upstream_status`(無則 null)
- `ai_rec.quota_denied`(INFO):`user_id`、`used`、`limit`、`period`
- `ai_rec.in_progress_denied`(INFO):`user_id`、`event_id`
- `ai_rec.unavailable`(ERROR):`user_id`、`event_id`

#### Scenario: 成功事件
- **WHEN** 一次推薦成功
- **THEN** 輸出一行可被解析的 JSON,`event` 為 `ai_rec.succeeded`,`latency_ms` 與 `cost_usd` 為數字

#### Scenario: 上游失敗事件
- **WHEN** 外部服務逾時
- **THEN** 輸出 `event` 為 `ai_rec.failed`、`error_code` 為逾時代碼的 JSON log,且包含例外資訊

#### Scenario: log 不含使用者輸入
- **WHEN** 請求帶有 `customPrompt`
- **THEN** 該次產生的所有 log 行都不包含 `customPrompt` 的內容

### Requirement: 查詢當月額度

系統 SHALL 提供 `GET /api/me/ai-recommendation-quota/`,回傳已登入使用者當月的額度狀態:`period`(`YYYY-MM`)、`limit`、`used`、`remaining`、`available`(是否還能使用)、`resetsAt`(下個月 1 日 00:00 台灣時間,ISO 8601)、`serviceAvailable`(外部服務是否已設定)。推薦成功的回應中 `quota` 欄位 SHALL 使用相同結構,並反映本次計次後的狀態。

#### Scenario: 查詢額度
- **WHEN** 已登入使用者當月成功使用 3 次後查詢
- **THEN** 回傳 `limit: 20`、`used: 3`、`remaining: 17`、`available: true`

#### Scenario: 額度用完時查詢
- **WHEN** 已用 20 次後查詢
- **THEN** `remaining: 0`、`available: false`

#### Scenario: 未設定外部服務時仍可查詢
- **WHEN** 伺服器未設定 Perplexity API key
- **THEN** 查詢成功回傳 200,`serviceAvailable: false`

#### Scenario: 未登入
- **WHEN** 未帶有效 access token 查詢
- **THEN** 回傳 401
