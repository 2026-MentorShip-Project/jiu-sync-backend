## ADDED Requirements

### Requirement: 應用程式的資料庫連線經過連線池

正式環境中,處理 API 請求的應用程式與背景任務 worker SHALL 經由連線池連線資料庫,SHALL NOT 直接連線資料庫。連線池 SHALL 限制對資料庫實際開啟的連線數上限;超出上限的請求 SHALL 排隊等待而非直接開新連線。連線池 SHALL 只在內部網路可連,不對外暴露。系統在連線池下 SHALL 維持既有的交易與鎖定行為(同一交易內的鎖與交易層級設定有效)。

#### Scenario: 應用程式經連線池存取資料庫
- **WHEN** 正式環境收到需要讀寫資料庫的 API 請求
- **THEN** 請求正常完成,且資料庫端看到的連線來源是連線池而非應用程式

#### Scenario: 大量併發不超過資料庫連線上限
- **WHEN** 同時進行的請求數超過連線池設定的資料庫連線上限
- **THEN** 資料庫實際連線數不超過上限,多出的請求排隊後完成,不因連線數耗盡而失敗

#### Scenario: 交易內的鎖定在連線池下仍有效
- **WHEN** 經連線池執行的測試套件包含依賴交易內鎖定的併發測試
- **THEN** 測試全部通過

#### Scenario: 連線池不對外暴露
- **WHEN** 從 EC2 外部嘗試連線連線池的 port
- **THEN** 連線失敗

### Requirement: 資料庫 migration 直連資料庫

部署時執行的資料庫 migration SHALL 直接連線資料庫,不經過連線池。直連所需的設定缺少時,部署 SHALL 明確失敗並中止,SHALL NOT 改用連線池執行 migration。

#### Scenario: 部署時 migration 直連
- **WHEN** 執行部署腳本且直連設定存在
- **THEN** migration 以直連方式執行成功

#### Scenario: 缺少直連設定
- **WHEN** 執行部署腳本但正式環境設定缺少直連資料庫的連線字串
- **THEN** 部署在 migration 前失敗並輸出明確錯誤,不執行 migration

### Requirement: 背景任務在正式環境實際被執行

正式環境 SHALL 運行背景任務 worker,使 API 排入的背景任務(例如通知信)被實際執行。應用程式與 worker SHALL 連線到正式環境內部的訊息佇列服務。

#### Scenario: 建立活動後通知任務被執行
- **WHEN** 正式環境建立一個活動
- **THEN** worker 取得並執行對應的通知任務,worker log 顯示任務完成

#### Scenario: 留言防洗版在正式環境生效
- **WHEN** 同一來源在 2 秒內對同一活動連續送出兩則合法留言
- **THEN** 第二則被拒絕
