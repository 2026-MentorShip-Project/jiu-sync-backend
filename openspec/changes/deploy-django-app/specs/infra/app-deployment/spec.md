## Purpose

定義把 Django 後端部署到 EC2 上必須具備的可觀察行為:對外有真實可信任的 HTTPS、部署動作可重複執行且會正確更新到最新版本、缺少必要設定時要明確失敗而非悄悄用預設值頂替。

## ADDED Requirements

### Requirement: 對外服務透過瀏覽器信任的 HTTPS 提供
使用者 SHALL 能透過瀏覽器信任(非自簽)的 HTTPS 連線存取後端服務,不need額外手動信任憑證。

#### Scenario: HTTPS 連線成功且憑證受信任
- **WHEN** 對部署後的公開網址(`https://<elastic-ip>.sslip.io`)發起 HTTPS 請求
- **THEN** TLS handshake 成功,憑證鏈可被標準瀏覽器/HTTP client(不需額外安裝信任根憑證)驗證通過

#### Scenario: Plain HTTP 請求被導向或拒絕
- **WHEN** 對該網址以 plain HTTP(port 80)發起請求
- **THEN** 不以明文回傳應用程式資料(導向 HTTPS,或明確拒絕/回應非應用層內容皆可,但不可讓 API 回應以明文 HTTP 傳輸)

### Requirement: 對外請求由 Django 後端實際處理
透過對外網址發出的 API 請求 SHALL 被路由到 Django 後端服務處理,而非 nginx 預設頁面或連線失敗。

#### Scenario: 已知 API 路徑回傳應用層回應
- **WHEN** 對 `https://<elastic-ip>.sslip.io/api/me/` 在未帶認證的情況下發出 GET 請求
- **THEN** 收到本專案定義的 401 錯誤格式(`{"message": ..., "code": ...}`),而非 nginx 預設錯誤頁、connection refused,或逾時

### Requirement: 應用程式 port 不直接對外暴露
Django/gunicorn 所在的 container SHALL 只能透過 nginx 存取,其監聽的 port 不可從 instance 外部直接連線。

#### Scenario: 直接連應用程式 port 被拒絕
- **WHEN** 從 instance 網路之外,直接對其公有 IP 的應用程式 port(例如 8000)發起連線
- **THEN** 連線被拒絕或逾時,無法繞過 nginx 直接連到應用程式

### Requirement: 部署動作可重複執行且會套用最新程式碼
重新執行部署流程 SHALL 讓服務更新到當下版本控制系統中的最新程式碼,且不需人工介入清理前次部署留下的狀態。

#### Scenario: 修改程式碼後重新部署,新版本生效
- **WHEN** 程式碼有變更並提交後,對已部署過的環境重新執行部署流程
- **THEN** 服務重新啟動後的行為反映新程式碼的變更(例如新版本回應內容可被觀察到與變更前不同),且過程不需要先手動移除或還原前次部署的任何資源

#### Scenario: 無變更時重新部署,服務維持正常可用
- **WHEN** 程式碼沒有任何變更的情況下,重新執行部署流程
- **THEN** 服務執行完畢後仍正常回應請求,不因重複部署而產生錯誤或服務中斷超出必要的重啟時間

### Requirement: 缺少必要環境設定時部署明確失敗
部署流程在偵測到必要的環境設定(例如正式環境的 secrets 檔案)不存在時,SHALL 明確中止並回報錯誤,不可用預設值、空值或靜默略過的方式繼續執行。

#### Scenario: 必要設定檔缺失時中止部署
- **WHEN** 執行部署流程時,EC2 instance 上預期存放 secrets 的檔案路徑不存在該檔案
- **THEN** 部署流程回報明確錯誤並中止,不會以任何預設/空值繼續啟動服務
