## Context

`terraform-ec2-provision`(已完成)提供:一台 running 的 EC2 instance(`i-0f6d5dc974e91bbf6`,Ubuntu 24.04,`ap-northeast-3`)、Elastic IP(`56.155.65.244`,重啟不變)、Security Group(僅 80/443 對外、無 SSH)、SSM 管理權限(instance 端與操作端皆已就緒)。本機開發用的 `docker-compose.yml`(db+redis)與正式環境無關,保持不動。詳細動機見 proposal.md - Why。

## Goals / Non-Goals

**Goals:**
- Django app 容器化,可在 EC2 上用 docker compose 跑起來
- nginx 提供 TLS 終止與 reverse proxy,對外只有 nginx 可見
- 不買網域也能有瀏覽器信任的 HTTPS(`sslip.io` + Let's Encrypt)
- 部署流程本機一鍵觸發、透過 SSM 執行、可重複執行更新版本

**Non-Goals:**
- CI/CD 自動化(GitHub Actions 觸發部署)——未來獨立 change
- Image registry(Docker Hub/ECR)——目前 EC2 上直接 clone + build
- 正式網域購買與 DNS 切換——未來獨立 change
- Secrets 自動化管理(Parameter Store/Secrets Manager)——目前手動管理
- 修改任何 `terraform-ec2-provision` 建立的 `.tf` 資源

## Decisions

### 1. 範圍一次全包,不拆 change
Dockerfile、prod compose、nginx、TLS、部署腳本彼此依賴度高(部署腳本要測,必須先有能跑的 image/compose;nginx 設定要測,必須先有 app 在監聽),拆開會產生「還無法完整驗收」的中間狀態,不符合本專案 task 拆分規則對「垂直切」的要求。

### 2. Dockerfile
沿用使用者提供的 uv 官方建議寫法,修正兩處:
- `CMD` 的 WSGI 路徑改為 `config.wsgi:application`(此專案實際路徑,原範本的 `core.wsgi` 是筆誤)
- `pyproject.toml` 新增 `gunicorn` 至 `dependencies`(目前不存在,prod 執行必須要有)

Build 指令固定帶 `--build-arg ENV=prod`,略過 `pytest`/`ruff` 等 dev-only 套件,image 更小,對 `t2.micro` 的 1GB RAM 較友善。

**替代方案考慮**:multi-stage build(builder stage 裝套件,runtime stage 只複製 venv)——否決,uv 的 cache mount 寫法已經有類似效果(cache 不進最終 image layer),沒有明顯多層 build 帶來的體積差異,且原範本已是官方建議寫法,不需要疊加額外複雜度。

### 3. nginx:host native,不進 docker compose
裝在 EC2 host 上(`apt install nginx certbot python3-certbot-nginx`),reverse proxy 指到 `127.0.0.1:8000`(app container 的 port mapping 只綁 loopback,不對外)。

**替代方案考慮**:nginx 也放進 docker compose——否決,host native 讓 certbot 的 `--nginx` plugin 可以直接自動改 nginx 設定檔、自動排定憑證續期的 systemd timer(Ubuntu 的 `certbot` apt 套件內建),不用額外處理「憑證檔案怎麼掛進 container」的問題,對單人維護的除錯路徑最短。

### 4. TLS:sslip.io + certbot
- Elastic IP `56.155.65.244` 對應網址為 `56-155-65-244.sslip.io`(`sslip.io` 的萬用 DNS:網址裡帶連字號分隔的 IP 會自動解析回該 IP,不需要註冊、不需要自己管 DNS)
- nginx `server_name` 設為該網址
- 用 `certbot --nginx -d 56-155-65-244.sslip.io --non-interactive --agree-tos -m <email>` 申請憑證,`-m` 需要一個 email(用於憑證到期提醒,非本專案業務 email,可用開發者自己的 email);certbot apt 套件會自動裝好定期續期的 systemd timer,不需要額外寫 cron
- **風險**:若之後 Elastic IP 因任何原因變更(例如 EIP 被釋放又重新分配到不同 IP),這個網址與憑證都會失效,需要重新跑一次 certbot 流程。這是 `sslip.io` 方案的已知限制,買正式網域可以避免(那是未來 change 的範圍)

### 5. Image 來源:git clone + 現場 build
EC2 上維持一份 repo clone(路徑建議 `/opt/jiu-sync-backend`),部署腳本每次執行時 `git pull`(首次 `git clone`),接著 `docker compose build`——Docker 自己的 layer cache 讓無變更時的 build 幾乎是 no-op,不需要額外手動比對「有沒有變更才 build」的邏輯,簡化冪等性設計。

**Git 認證**:此 repo 若為 private repository,EC2 上的 `git clone`/`git pull` 需要認證。比照決策 6 的哲學(secrets 由使用者手動處理,部署腳本不管理):使用者需自行在 EC2 上(透過一次性 SSM session)設定好 git 認證(例如 fine-grained GitHub PAT 寫入 `~/.git-credentials`,或設定 SSH deploy key),部署腳本假設這一步已完成,不在腳本裡處理、不經手 token。

### 6. Secrets:使用者手動管理,腳本不碰
`.env` 由使用者透過一次性 SSM session 手動在 EC2 上建立,固定路徑(`/opt/jiu-sync-backend/.env`,與 repo clone 同目錄,`docker-compose.prod.yml` 用 `env_file: .env` 讀取)。部署腳本執行前檢查該檔案是否存在,不存在則明確報錯中止(對應 spec 的「缺少必要環境設定時部署明確失敗」需求),不自動產生、不填預設值。

### 7. 部署腳本:本機執行,SSM send-command 觸發
`infra/scripts/deploy.sh`,使用者在自己電腦手動執行(`terraform apply` 完成後的獨立第二步),不是 Terraform provisioner。腳本邏輯:
1. 組出要在遠端執行的 shell script 字串(check `.env` 存在 → git pull-or-clone → docker compose build → docker compose up -d → migrate → collectstatic)
2. `aws ssm send-command --instance-ids <id> --document-name AWS-RunShellScript --parameters commands=[...]` 送出
3. Poll `aws ssm get-command-invocation` 直到 `Status` 為終態(`Success`/`Failed`/`TimedOut`/`Cancelled`)
4. 印出遠端執行的 stdout/stderr,`Success` 以外的狀態讓腳本以非 0 exit code 結束(可被 CI 或使用者的其他自動化偵測失敗)

不使用 SSH:instance 的 Security Group 沒開 port 22,也沒有 SSH key pair(`terraform-ec2-provision` 的既有設計),全部管理走 SSM。操作用的 IAM User 已在該 change 的 task 4.2 補齊 `ssm:SendCommand`/`ssm:GetCommandInvocation` 權限。

### 8. 冪等性
不特別設計「偵測有無變更才動作」的邏輯——`git pull` 沒有新 commit 時是 no-op,`docker compose build` 沒有 layer 變更時幾乎瞬間完成(cache hit),`docker compose up -d` 沒有 image/設定變更的 service 不會被重啟。整個流程天然冪等,重跑安全,不需要額外的版本比對機制。

## Risks / Trade-offs

- **[風險] `t2.micro` 1GB RAM 同時跑 app+db+redis+nginx+build 過程,build 當下記憶體壓力最大** → 已在 `terraform-ec2-provision` 的 grill-me 明確接受此風險(先試跑觀察);若部署腳本執行中因 OOM 失敗,SSM command 會回報非 Success 狀態,不會是靜默失敗
- **[風險] `sslip.io` 依賴 Elastic IP 不變** → 已於決策 4 說明,IP 變動需要重新申請憑證,這是接受的已知限制
- **[風險] git 認證(private repo)不在此 change 自動化範圍內** → 使用者需自行於 EC2 上一次性設定,部署腳本假設已完成;若未設定,`git pull`/`git clone` 會在 SSM command 執行時明確失敗並回報,不會靜默略過
- **[風險] 部署腳本透過 SSM 執行,單次 `send-command` 的輸出/執行時間有 AWS 內建上限**(command 輸出 truncate、預設 timeout)→ 若 build 時間隨依賴增加而變長導致逾時,需要調整 `--timeout-seconds` 參數;目前先用預設值驗證,真的卡住再調整

## Migration Plan

- **首次部署**:確認 `terraform-ec2-provision` 的 EC2 已 running、`.env` 已手動建立、git 認證已設定,執行 `./infra/scripts/deploy.sh`
- **後續更新**:code 變更 commit/push 後,直接重跑同一支 `deploy.sh`
- **Rollback**:目前無自動 rollback——若新版本部署後發現問題,`git revert`/`git checkout` 回前一個 commit 後重跑 `deploy.sh` 即可回到前一版行為(依賴決策 8 的冪等性設計);資料庫 migration 若不可逆,需要額外手動處理,不在此 change 自動化範圍內
