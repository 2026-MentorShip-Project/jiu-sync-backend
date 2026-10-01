# 8. UML:使用者流程與後端 API 對照

本章用 UML 的**活動圖**(使用者流程)與**狀態機圖**(活動生命週期),把產品流程的每一步對應到前端要呼叫的後端 API,方便前後端串接與檢查。

- 所有路徑都以 `/api` 開頭,而且**結尾都有斜線**(例如 `/api/events/{id}/poll/`)。
- `{id}` 是 8 碼 base62 的活動 id;`{responseId}`、`{commentId}` 也是 8 碼 base62。
- 🔒 代表需要 `Authorization: Bearer <access token>`;🔓 代表免登入。
- 「前端處理」代表這一步不需要呼叫後端。

## 8.1 主揪流程

```mermaid
flowchart TB
    subgraph H1["1 · 登入"]
        H1a["POST /api/auth/google/ 🔓<br/>Google id_token → access token + refresh cookie"]
        H1b["GET /api/me/ 🔒<br/>取得目前登入者"]
    end
    subgraph H2["2 · 我的活動"]
        H2a["GET /api/events/?owner=me 🔒"]
    end
    subgraph H3["3 · 建立活動"]
        H3a["POST /api/events/ 🔒<br/>回傳 {id, shareUrl},背景寄分享連結給自己"]
        H3b["前端處理:複製 / 分享連結"]
    end
    subgraph H4["4 · 活動管理頁"]
        H4a["GET /api/events/{id}/ 🔒<br/>完整資料、投票彙整、isOwner"]
        H4b["PATCH /api/events/{id}/ 🔒<br/>編輯基本資料(僅進行中)"]
        H4c["GET /api/events/{id}/poll/ 🔓<br/>每 10 秒,有變化才重拉"]
    end
    subgraph H5["5 · 活動決策"]
        H5a["POST /api/events/{id}/finalize/ 🔒"]
        H5b["POST /api/events/{id}/cancel/ 🔒"]
        H5c["POST /api/events/{id}/reopen/ 🔒"]
    end
    subgraph H6["6 · 留言管理"]
        H6a["GET /api/events/{id}/comments/?cursor= 🔓"]
        H6b["DELETE /api/events/{id}/comments/{commentId}/ 🔒"]
    end
    subgraph H7["7 · AI 餐廳推薦(定案後)"]
        H7a["GET /api/me/ai-recommendation-quota/ 🔒<br/>剩餘次數、服務是否可用"]
        H7b["POST /api/events/{id}/restaurant-recommendations/ 🔒"]
        H7c["PUT /api/events/{id}/selected-restaurant/ 🔒"]
    end
    subgraph H8["8 · 登出"]
        H8a["POST /api/auth/logout/ 🔒"]
    end

    H1 --> H2
    H2 -- "建立新活動" --> H3
    H2 -- "點選既有活動" --> H4
    H3 --> H4
    H4 --> H5
    H4 --> H6
    H5 -- "定案成功" --> H7
    H5 -- "重新開放" --> H4
    H7 --> H4
    H4 --> H8
```

**access token 過期時(任一步驟收到 401):** 前端呼叫 `POST /api/auth/refresh/` 🔓(瀏覽器自動帶 refresh cookie),拿到新的 access token 後重送原本的請求;refresh 也失敗時,回到步驟 1 重新登入。

## 8.2 參與者流程

```mermaid
flowchart TB
    subgraph P1["A · 開啟分享連結"]
        P1a["GET /api/events/{id}/ 🔓<br/>活動資料與投票彙整"]
        P1b["410 LINK_EXPIRED → 顯示連結已失效"]
    end
    subgraph P2["B · 首次投票"]
        P2a["POST /api/events/{id}/responses/ 🔓<br/>暱稱 + 末三碼 + 每個時段的表態"]
    end
    subgraph P3["C · 修改投票"]
        P3a["POST /api/events/{id}/responses/verify/ 🔓<br/>核對身分 → 30 分鐘、一次性的 accessToken + responseId"]
        P3b["PATCH /api/events/{id}/responses/{responseId}/ 🔓<br/>body 帶 accessToken"]
    end
    subgraph P4["D · 留言板"]
        P4a["GET /api/events/{id}/comments/?cursor= 🔓<br/>每頁 10 則,由新到舊"]
        P4b["POST /api/events/{id}/comments/ 🔓<br/>同 IP 同活動 2 秒一則"]
    end
    subgraph P5["E · 等待結果"]
        P5a["GET /api/events/{id}/poll/ 🔓<br/>每 10 秒"]
        P5b["前端處理:比對摘要,有變化才重拉 A 或 D"]
    end

    P1 -- "第一次來" --> P2
    P1 -- "已經投過" --> P3
    P1 --> P4
    P2 --> P5
    P3 --> P5
    P4 --> P5
    P5 -- "有變化" --> P1
```

**前置條件(B、C 共用):** 連結失效回 410 `LINK_EXPIRED` → 活動不是進行中回 409 `EVENT_NOT_ACTIVE` → 已過截止時間回 409 `VOTING_CLOSED`。留言(D)只檢查連結是否失效。

## 8.3 活動生命週期(狀態機)

```mermaid
stateDiagram-v2
    [*] --> active : POST /api/events/
    active --> finalized : POST finalize/(選定時段)
    active --> cancelled : POST cancel/
    finalized --> active : POST reopen/(新的截止時間)
    finalized --> cancelled : POST cancel/
    cancelled --> [*]

    state active {
        voting_open --> voting_closed_pending : 超過 response_deadline
    }
    state finalized {
        finalized_upcoming --> finalized_past : 超過聚會日期(台灣時間)
    }
```

- 外層的 `active`、`finalized`、`cancelled` 是資料庫的 `status`;內層是 API 回傳的 `displayStatus`,由時間推算,不寫入資料庫。
- 定案或取消超過 7 天,公開端點一律回 410,`displayStatus` 為 `link_expired`;主揪的取消與重新開放不受影響。
- 每個轉換都是條件式 UPDATE:狀態不符時回 409(例如重複定案回 `EVENT_ALREADY_FINALIZED`),不會發生兩個請求同時轉換成功。
- 定案、取消、重新開放成功後,背景寄通知信給留過 email 的參與者與主揪。
- AI 推薦與選定餐廳只在 `finalized_upcoming` 時可用;投票只在 `voting_open` 時可用。

## 8.4 API 一覽

| 步驟 | 方法與路徑 | 認證 | 誰能用 |
| --- | --- | --- | --- |
| 主揪 1 | `POST /api/auth/google/` | 🔓 | 任何人(用 Google id_token) |
| 主揪 1 | `GET /api/me/` | 🔒 | 已登入主揪 |
| 全部 | `POST /api/auth/refresh/` | refresh cookie | 已登入主揪 |
| 主揪 8 | `POST /api/auth/logout/` | 🔒 | 已登入主揪 |
| 主揪 2 | `GET /api/events/?owner=me` | 🔒 | 已登入主揪 |
| 主揪 3 | `POST /api/events/` | 🔒 | 已登入主揪 |
| 主揪 4、參與者 A | `GET /api/events/{id}/` | 選填 | 任何人;擁有者多看到 `hostEmail` |
| 主揪 4 | `PATCH /api/events/{id}/` | 🔒 | 擁有者 |
| 主揪 4、參與者 E | `GET /api/events/{id}/poll/` | 🔓 | 任何人 |
| 參與者 B | `POST /api/events/{id}/responses/` | 🔓 | 任何人 |
| 參與者 C | `POST /api/events/{id}/responses/verify/` | 🔓 | 任何人 |
| 參與者 C | `PATCH /api/events/{id}/responses/{responseId}/` | accessToken | 投票本人 |
| 主揪 6、參與者 D | `GET /api/events/{id}/comments/` | 🔓 | 任何人 |
| 參與者 D | `POST /api/events/{id}/comments/` | 🔓 | 任何人 |
| 主揪 6 | `DELETE /api/events/{id}/comments/{commentId}/` | 🔒 | 擁有者 |
| 主揪 5 | `POST /api/events/{id}/finalize/` | 🔒 | 擁有者 |
| 主揪 5 | `POST /api/events/{id}/cancel/` | 🔒 | 擁有者 |
| 主揪 5 | `POST /api/events/{id}/reopen/` | 🔒 | 擁有者 |
| 主揪 7 | `GET /api/me/ai-recommendation-quota/` | 🔒 | 已登入主揪 |
| 主揪 7 | `POST /api/events/{id}/restaurant-recommendations/` | 🔒 | 擁有者 |
| 主揪 7 | `PUT /api/events/{id}/selected-restaurant/` | 🔒 | 擁有者 |

非產品 API:`GET /healthz/`(健康檢查)、`GET /metrics`(需 `METRICS_TOKEN`,對外由 nginx 封鎖)、`/admin/`(Django 管理後台)。
