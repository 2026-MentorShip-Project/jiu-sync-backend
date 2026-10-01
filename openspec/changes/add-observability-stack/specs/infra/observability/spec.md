## Purpose

讓正式環境的應用程式、container 與主機狀態可以被持續觀察:收集 metrics 與 log 並送到外部觀測平台,以 dashboard 與告警呈現,且觀測系統本身的故障不影響應用程式。

## ADDED Requirements

### Requirement: 應用程式提供 HTTP 層 metrics

應用程式 SHALL 提供 metrics 端點,輸出每個 view 的請求數、回應時間分布、HTTP status 分布。應用程式以多個 worker process 運行時,端點輸出的數值 SHALL 為所有 worker 的加總,SHALL NOT 隨請求落到哪個 worker 而變動。worker 重啟後,先前 worker 已累計的 counter SHALL 保留在加總中;應用程式整體重啟後 SHALL 從零開始,不殘留上次啟動的數值。

#### Scenario: 多個 worker 的數值被加總
- **WHEN** 多個 worker 各自處理過請求後讀取 metrics 端點多次
- **THEN** 每次讀到的總請求數一致,且等於所有 worker 處理的請求總數

#### Scenario: 應用程式重啟後不殘留舊數值
- **WHEN** 應用程式重啟後第一次讀取 metrics 端點
- **THEN** 請求數不包含重啟前的累計值

### Requirement: metrics 端點只對授權的收集端開放

metrics 端點 SHALL 只在請求帶有與設定相符的 bearer token 時回傳 metrics。未帶 token、token 不符、或系統未設定 token 時,端點 SHALL 回應 404,回應內容 SHALL 與不存在的路徑相同,SHALL NOT 透露端點存在。從公開網址存取 metrics 路徑 SHALL 得到 404。

#### Scenario: 正確 token
- **WHEN** 請求帶有正確的 bearer token
- **THEN** 回應 200 與 metrics 內容

#### Scenario: 未帶 token 或 token 錯誤
- **WHEN** 請求未帶 token,或帶的 token 不符
- **THEN** 回應 404,內容與不存在路徑相同

#### Scenario: 系統未設定 token(fail closed)
- **WHEN** 系統未設定 metrics token,請求帶任意 token 或不帶
- **THEN** 回應 404

#### Scenario: 內部收集端以 http 存取
- **WHEN** 內部收集端以 http 經 compose 內部網址(主機名稱 `app`)存取 metrics 端點並帶正確 token
- **THEN** 回應 200,不被 https 導向或主機名稱檢查擋下

#### Scenario: 從公開網址存取
- **WHEN** 從 EC2 外部經公開網址存取 metrics 路徑
- **THEN** 回應 404

### Requirement: 收集 container 與主機的 metrics 及 log

正式環境 SHALL 收集並送至外部觀測平台:應用程式 metrics、每個 container 的 CPU / 記憶體 / 網路 / 重啟、主機的 CPU / 記憶體 / swap / 磁碟 / 網路,以及所有 container 的 stdout/stderr log 與主機 nginx 的 access / error log。每筆 log SHALL 可依來源(container 名稱或 nginx log 類型)區分。送出的 active series 數 SHALL 控制在外部平台免費額度內。

#### Scenario: 各層資料在平台可查
- **WHEN** 部署完成數分鐘後在外部平台查詢
- **THEN** 查得到應用程式、各 container、主機三層 metrics,以及各 container 與 nginx 的 log

#### Scenario: series 數在額度內
- **WHEN** 部署完成後查看外部平台的 active series 用量
- **THEN** 低於 3,000

### Requirement: 送出的 log 遮蔽敏感資料

送至外部平台的 log SHALL 將 bearer token、JWT 與 email 位址替換為遮蔽字串。來源 IP SHALL 保留。

#### Scenario: log 含敏感字串
- **WHEN** 某行 log 含 `Bearer <token>`、JWT 或 email
- **THEN** 外部平台上該行的對應部分為遮蔽字串,其餘內容不變

#### Scenario: log 含 IP
- **WHEN** nginx access log 含來源 IP
- **THEN** 外部平台上保留原 IP

### Requirement: 收集重啟不重送也不遺漏 log

收集元件重啟(含每次部署)後 SHALL 從上次讀取的位置繼續,SHALL NOT 重送已送出的 log,SHALL NOT 遺漏重啟期間寫入檔案的 log。外部平台暫時無法連線時,metrics SHALL 暫存並於恢復後補送。

#### Scenario: 部署後 nginx log 不重送
- **WHEN** 部署導致收集元件重啟
- **THEN** 外部平台上重啟前已送出的 nginx log 沒有重複

### Requirement: 觀測系統故障不影響應用程式

收集元件停止、被 OOM kill、或外部平台無法連線時,應用程式、worker 與資料庫 SHALL 正常運作。收集元件的記憶體用量 SHALL 有上限。正式環境缺少外部平台設定時,部署 SHALL 輸出警告並繼續完成應用程式部署,SHALL NOT 中止。

#### Scenario: 收集元件停止
- **WHEN** 收集元件被停止
- **THEN** API 請求照常成功

#### Scenario: 缺少外部平台設定時部署
- **WHEN** 執行部署且正式環境設定缺少外部平台的連線資訊
- **THEN** 部署輸出警告,應用程式照常部署完成

### Requirement: Dashboard 與告警

外部平台 SHALL 提供應用程式、container、主機、log 四個 dashboard,並在以下情況寄送告警:收集資料中斷超過 10 分鐘、主機可用記憶體低於門檻、5xx 比例超過 5%、任一 container 重啟。dashboard 與告警規則 SHALL 匯出保存於 repo。

#### Scenario: 收集中斷觸發告警
- **WHEN** 收集元件停止超過 10 分鐘
- **THEN** 收到告警 email
