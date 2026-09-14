## Why

揪甘心後端目前只能在開發者本機跑,沒有任何可長期存取的部署環境,前端團隊每次要做真實整合測試都得依賴臨時的 ngrok tunnel。要有一個穩定、可重複建立、成本可控的正式部署環境,第一步是先把運算資源(EC2)用 Terraform 建起來,讓後續(nginx、docker compose、應用程式部署)有地方可以裝。

## What Changes

- 新增 Terraform 設定,於 AWS `ap-northeast-3`(大阪)建立一台 `t2.micro` EC2 instance(Ubuntu 24.04 LTS),供後續部署後端服務使用
- 使用該 region 的預設 VPC/subnet,不自建網路資源
- 為 instance 建立/關聯一個 Elastic IP,使關機重開後對外 IP 維持固定
- 建立 Security Group:開放 inbound 80(HTTP)、443(HTTPS)給 0.0.0.0/0;不開放 22(SSH)
- 建立一個 IAM Role(instance profile),附加受管 policy `AmazonSSMManagedInstanceCore`,並關聯到這台 instance,讓開發者可透過 AWS Systems Manager Session Manager 連線管理,不依賴 SSH key pair
- Terraform state 採用本機 local backend,並確保 `.gitignore` 排除 state 檔
- 不包含:instance 內部的 nginx/docker/docker compose 安裝與設定、應用程式部署、CI/CD 整合——這些留給後續 change

## Capabilities

### New Capabilities
- `infra/ec2-provisioning`: 用 Terraform 宣告式管理部署用 EC2 運算資源(instance、Elastic IP、Security Group、SSM 用 IAM Role),使其可重複建立/銷毀,且不依賴 SSH key pair 存取

### Modified Capabilities

(無 — 此 change 不修改任何既有 capability 的需求)

## Impact

- 新增檔案:`infra/terraform/` 目錄下的 `.tf` 設定檔(provider、instance、network、security group、IAM role、outputs)
- 新增:`.gitignore` 需補上 Terraform state/暫存檔案規則(`*.tfstate`、`*.tfstate.*`、`.terraform/`)
- 新的外部依賴:本機需安裝 Terraform CLI;AWS 帳號需有一組掛 `AmazonEC2FullAccess` 的 IAM User 憑證(開發者已手動於 AWS Console 建立,不由 Terraform 管理)
- 產生的雲端費用:EC2 `t2.micro`(依 free tier 額度)+ Elastic IP(依 2024 年後 AWS 公有 IPv4 計費規則,約 USD 0.005/小時)
- 不影響現有 Django 後端程式碼、API 行為
