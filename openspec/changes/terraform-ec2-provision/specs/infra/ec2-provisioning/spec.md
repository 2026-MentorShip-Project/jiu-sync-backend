## Purpose

定義部署用 EC2 運算資源必須具備的可觀察行為:可重複建立、不依賴 SSH key 存取、關機重開後對外位址維持固定、對外網路暴露面最小化。

## ADDED Requirements

### Requirement: 運算資源可透過 Infrastructure as Code 重複建立與銷毀
部署用的 EC2 instance 及其關聯資源(網路位址、存取權限、防火牆規則)SHALL 以宣告式設定管理,執行建立指令後可得到與設定一致的資源狀態,執行銷毀指令後資源 SHALL 完全移除、不殘留孤兒資源。

#### Scenario: 首次建立
- **WHEN** 對一個尚未建立任何資源的環境執行建立指令
- **THEN** 產生一台指定規格(`t2.micro`、Ubuntu 24.04 LTS、`ap-northeast-3`)的 EC2 instance,且指令執行結果回報成功、無需人工修正

#### Scenario: 重複執行不造成資源重複
- **WHEN** 在資源已存在、且設定未變更的情況下,再次執行建立指令
- **THEN** 不會產生第二台 instance 或重複的關聯資源,指令回報「無需變更」

### Requirement: 不依賴 SSH key pair 即可管理 instance
維運者 SHALL 能在不持有、不分發任何 SSH 私鑰的情況下連線進入 instance 執行指令。

#### Scenario: 透過 Session Manager 連線
- **WHEN** 維運者對已建立的 instance 發起 Session Manager 連線請求
- **THEN** 連線成功並取得該 instance 的 shell,過程不需提供或使用任何 SSH key

#### Scenario: SSH port 對外不可達
- **WHEN** 從 instance 所在網路之外,嘗試以 TCP port 22 連線到該 instance 的公開位址
- **THEN** 連線被拒絕或逾時,無法建立 SSH 連線

### Requirement: 對外公有 IP 位址在 instance 重啟後維持不變
instance 關閉後再重新啟動,SHALL 沿用同一個對外公有 IP 位址,不因開關機而變動。

#### Scenario: 關機後重開,IP 不變
- **WHEN** 對已建立的 instance 執行關機、再執行開機
- **THEN** 開機完成後對外可達的公有 IP 位址與關機前相同

### Requirement: 對外網路暴露面僅限 HTTP 與 HTTPS
instance 的防火牆規則 SHALL 只允許 inbound TCP 80(HTTP)與 443(HTTPS)從任意來源進入,不允許其他 inbound port(包含 22)對外開放。

#### Scenario: 80/443 可連線
- **WHEN** 從 instance 網路之外,以 TCP 對其公有 IP 的 port 80 或 443 發起連線
- **THEN** 連線在網路層可達(TCP handshake 成功,即使該 port 上尚未有服務監聽而被 RST/connection refused,也視為防火牆層允許通過)

#### Scenario: 非 80/443/SSM 所需 port 一律不可達
- **WHEN** 從 instance 網路之外,以 TCP 對其公有 IP 的任意非 80、443 的 port 發起連線
- **THEN** 連線在防火牆層被阻擋(逾時或明確拒絕),無法建立連線

### Requirement: Terraform state 不進入版本控制
管理此資源的 Terraform state 檔案 SHALL 不被提交進 git 版本控制。

#### Scenario: state 檔案被 git 忽略
- **WHEN** 執行建立指令產生本機 state 檔案後,檢查 git 追蹤狀態
- **THEN** 該 state 檔案(及其備份、暫存檔)不出現在 `git status` 的可提交變更清單中
