## Context

目前完全沒有部署基礎設施,也是本專案第一次引入 Terraform 工具鏈。開發者已在 AWS Console 手動建立一組 IAM User(`AmazonEC2FullAccess`),Terraform 執行時以此憑證(環境變數 `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`)操作,不由 Terraform 自行管理這組憑證(避免 circular bootstrap)。詳細動機見 proposal.md - Why。

## Goals / Non-Goals

**Goals:**
- 用 Terraform 宣告式建立一台可運行未來 docker compose 部署的 EC2 instance
- 存取方式不依賴 SSH key pair
- 對外網路暴露面最小化(僅 80/443)
- 關機重開後對外位址不變

**Non-Goals:**
- instance 內部的 nginx / docker / docker compose 安裝與設定(後續 change)
- 應用程式部署、環境變數/secret 注入(後續 change)
- CI/CD 整合、Terraform 自動化執行(目前手動於本機執行 `terraform apply`)
- 多環境(staging/prod)管理——目前只有單一環境

## Decisions

### 1. Terraform 檔案佈局
路徑:`infra/terraform/`,採單一 root module(不拆 module),因為目前資源量小(instance + EIP + SG + IAM role),過度模組化對單人專案是不必要的抽象:

- `providers.tf`:`aws` provider,region 寫死 `ap-northeast-3`(非 variable——目前只有單一環境,不需要參數化)
- `data.tf`:`data "aws_vpc" "default"`(預設 VPC)、`data "aws_subnets"`(預設 VPC 下的 subnet)、`data "aws_ami"`(最新 Canonical 官方 Ubuntu 24.04 LTS,owner 過濾避免抓到非官方 AMI)
- `security_group.tf`:`aws_security_group`,inbound 80/443 from `0.0.0.0/0`,outbound all(SSM/apt/docker pull 都需要出網,egress 全開是業界對此情境的標準做法)
- `iam.tf`:`aws_iam_role`(assume role policy 限定 `ec2.amazonaws.com`)+ `aws_iam_role_policy_attachment`(`AmazonSSMManagedInstanceCore`)+ `aws_iam_instance_profile`
- `ec2.tf`:`aws_instance`,`t2.micro`,關聯上述 SG 與 instance profile,`root_block_device` 用 `gp3`、20GB(在 AWS free tier 30GB EBS 額度內,含未來 docker image 與 log 空間的合理預留;不是 grill-me 決策項,採合理預設)
- `eip.tf`:`aws_eip`(`domain = "vpc"`)直接關聯到該 instance
- `outputs.tf`:輸出 `instance_id`、`public_ip`,方便後續 change(裝 nginx、接網域)取用
- `.gitignore` 補上 `infra/terraform/.terraform/`、`*.tfstate`、`*.tfstate.*`、`*.tfvars`(若之後有機密變數)

**替代方案考慮**:拆成多個 module(network/compute/security)——否決,資源量不足以攤銷模組化的維護成本。

### 2. AMI 取得方式:`data "aws_ami"` 動態查詢,不寫死 AMI ID
AMI ID 因 region 而異、且會隨官方更新版本而變動。用 `data` source 依 owner(Canonical 官方帳號 ID)+ name pattern(`ubuntu-noble-24.04-amd64-server-*`)+ `most_recent = true` 動態抓,避免寫死過期或錯誤 region 的 AMI ID。

### 3. SSM 存取:IAM Role 而非 IAM User
第 grill-me 已定案:SSM 需要的是「instance 本身」的身份(instance profile 掛 Role),跟「Terraform 操作者」的身份(IAM User)是兩個不同主體,不可混用同一組憑證。

### 4. Elastic IP 直接用 `aws_eip.instance` 屬性關聯,不另建 `aws_eip_association`
兩者效果相同,但在只有單一 instance、非多重關聯情境下,直接在 `aws_eip` 資源上指定 `instance` 屬性語法更簡潔,少一個資源宣告。

## Risks / Trade-offs

- **[風險] `t2.micro` 記憶體(1GB)未來裝 Django+Postgres+Redis+Nginx 可能不足** → 已在 grill-me 明確決定「先試跑觀察」,若後續 change 實測 OOM,再回頭調整 instance type(這是已知、刻意接受的風險,非本次 change 需要解決)
- **[風險] Elastic IP 持續產生費用(~USD 0.005/小時)** → grill-me 已確認開發者接受此費用以換取 IP 穩定;若未來想完全避免,需改為每次重開手動更新 DNS(已在 grill-me 討論並否決)
- **[風險] 預設 VPC 若帳號被刪除或不存在(新帳號有時不會自動生成)會導致 `data` 查詢失敗** → 減緩:tasks.md 的環境健檢 task 需包含確認預設 VPC 存在;若不存在,需先於 Console 手動建立預設 VPC 再繼續(非本次 change 自動化範圍)
- **[風險] 防火牆全開 80/443 給 0.0.0.0/0,在尚未裝 nginx/TLS 前,這兩個 port 上沒有服務監聽** → 這是預期狀態(spec 已定義「connection refused 也視為防火牆層允許」),不是安全漏洞;實際服務要到後續 nginx change 才會啟動

## Migration Plan

- **部署**:`cd infra/terraform && terraform init && terraform plan && terraform apply`,人工檢視 `plan` 輸出後才 `apply`(不自動化,單人專案人工把關即可)
- **銷毀**(若要整個拆除重建):`terraform destroy`,因所有資源皆由 Terraform 管理且無跨資源手動修改,可完全乾淨銷毀
- **Rollback**:目前無自動 rollback 機制——若 `apply` 後發現設定錯誤,以修正設定後重新 `apply`(Terraform 的宣告式特性,收斂到期望狀態)取代手動 rollback;若徹底失敗,`terraform destroy` 後重新 `apply` 亦可接受(單一環境、無服務中斷疑慮,此階段機器上尚無運行中的應用程式)
