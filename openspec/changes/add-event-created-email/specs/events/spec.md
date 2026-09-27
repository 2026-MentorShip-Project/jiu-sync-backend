## ADDED Requirements

### Requirement: 建立活動成功後寄送分享連結通知信

系統 SHALL 在主揪成功建立活動（`POST /api/events`）後，非同步寄送一封通知信至主揪的 Email（`Event.host_email`），內容包含活動標題與分享連結。信件寄送失敗 SHALL NOT 影響建立活動這個動作本身已經成功的 API 回應。

#### Scenario: 建立活動成功後主揪收到分享連結信

- **WHEN** 已登入使用者成功呼叫 `POST /api/events` 建立一筆活動
- **THEN** 系統非同步寄送一封信到該使用者的 Email，內容包含活動標題與分享連結

#### Scenario: 通知信寄送失敗不影響建立活動的回應

- **WHEN** 建立活動的 API 已經成功寫入資料庫，但通知信寄送過程發生錯誤（例如 mail backend 連線失敗）
- **THEN** `POST /api/events` 仍回傳 201 與正常的活動內容，不會因為信件寄送失敗變成 500
