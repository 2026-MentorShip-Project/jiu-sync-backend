## 1. 環境健檢

- [x] 1.1 滿足需求「運算資源可透過 Infrastructure as Code 重複建立與銷毀」的前置條件:確認本機工具鏈與雲端存取就緒——`terraform version` 顯示已安裝版本、`aws sts get-caller-identity` 成功回傳掛 `AmazonEC2FullAccess` 的 IAM User ARN、`git status` 顯示在 `feature/Iac-about-deploy` branch 且無非預期未追蹤檔案、`aws ec2 describe-vpcs --region ap-northeast-3 --filters Name=is-default,Values=true` 回傳非空(確認大阪 region 有預設 VPC)。(auto)

## 2. Terraform 專案骨架與 State 排除

- [x] 2.1 滿足需求「Terraform state 不進入版本控制」:建立 `infra/terraform/providers.tf`(aws provider,region 固定 `ap-northeast-3`)與 `infra/terraform/data.tf`(`data "aws_vpc" "default"`、`data "aws_subnets"`、`data "aws_ami"` 查詢最新 Canonical 官方 Ubuntu 24.04 LTS),並於根目錄 `.gitignore` 補上 `infra/terraform/.terraform/`、`*.tfstate`、`*.tfstate.*`。驗證:`terraform init` 成功、`terraform plan` 能正確解析出 VPC ID 與 AMI ID(此階段無實際資源可建立,不執行 apply);`git status --ignored` 顯示 state/暫存檔落在忽略清單而非一般追蹤清單。(auto)

## 3. 存取控制資源(IAM Role + Security Group)

- [x] 3.1 滿足需求「不依賴 SSH key pair 即可管理 instance」與「對外網路暴露面僅限 HTTP 與 HTTPS」:建立 `infra/terraform/iam.tf`(`aws_iam_role` assume-role 限定 `ec2.amazonaws.com`、附加 `AmazonSSMManagedInstanceCore`、`aws_iam_instance_profile`)與 `infra/terraform/security_group.tf`(inbound 80/443 from `0.0.0.0/0`、outbound all,不開放 22)。驗證:`terraform apply` 成功;`aws iam get-role`、`aws iam list-attached-role-policies` 確認角色與 policy 附加正確;`aws ec2 describe-security-groups` 確認 inbound 規則僅 80/443、無 22。(auto)

## 4. EC2 Instance 與 Elastic IP

- [x] 4.1 滿足需求「運算資源可透過 Infrastructure as Code 重複建立與銷毀」:建立 `infra/terraform/ec2.tf`(`t2.micro`、data 查詢出的 Ubuntu 24.04 AMI、關聯 3.1 建立的 IAM instance profile 與 Security Group、`root_block_device` 為 gp3 20GB)、`infra/terraform/eip.tf`(`aws_eip` 直接關聯該 instance)、`infra/terraform/outputs.tf`(輸出 `instance_id`、`public_ip`)。驗證:`terraform apply` 成功建立 instance 與 Elastic IP,`terraform output` 正確顯示 `instance_id`、`public_ip`。(auto)
- [x] 4.2 滿足需求「不依賴 SSH key pair 即可管理 instance」:對 4.1 建立的 instance 執行 `aws ssm start-session --target <instance_id>`,連線成功並可執行指令(如 `whoami`)後結束 session;對其公有 IP 的 TCP port 22 執行 `nc -zv -w5 <ip> 22` 確認連線逾時或被拒絕。(auto)
- [x] 4.3 滿足需求「對外公有 IP 位址在 instance 重啟後維持不變」:記錄目前 `public_ip`,執行 `aws ec2 stop-instances` 並等待 `stopped` 狀態,再執行 `aws ec2 start-instances` 並等待 `running` 狀態,比對開機後的 `public_ip` 與關機前是否相同。(auto)
- [x] 4.4 滿足需求「對外網路暴露面僅限 HTTP 與 HTTPS」:對 instance 公有 IP 的 port 80、443 執行 `nc -zv -w5` 確認 TCP 層可達(connection refused 亦視為通過,因尚未安裝任何服務);對任一非 80/443 的 port(如 3389)執行同樣測試,確認逾時或被拒絕。(auto)

## 5. 冪等性驗證

- [x] 5.1 滿足需求「運算資源可透過 Infrastructure as Code 重複建立與銷毀」:在所有資源已建立且設定未變更的狀態下,再次執行 `terraform plan`,確認輸出為「No changes」,不產生新增、修改或替換的資源異動。(auto)
