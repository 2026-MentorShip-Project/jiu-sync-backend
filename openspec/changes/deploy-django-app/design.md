## Context

`terraform-ec2-provision`(已完成)提供:一台 running 的 EC2 instance(`i-0f6d5dc974e91bbf6`,Ubuntu 24.04,`ap-northeast-3`)、Elastic IP(`56.155.65.244`,重啟不變)、Security Group(僅 80/443 對外、無 SSH)、SSM 管理權限(instance 端與操作端皆已就緒)。本機開發用的 `docker-compose.yml`(db+redis)與正式環境無關,保持不動。詳細動機見 proposal.md - Why。

## Goals / Non-Goals

**Goals:**
- Django app 容器化,可在 EC2 上用 docker compose 跑起來
- nginx 提供 TLS 終止與 reverse proxy,對外只有 nginx 可見
- 不買網域也能有瀏覽器信任的 HTTPS(`sslip.io` + Let's Encrypt)
- 部署流程本機一鍵觸發、透過 SSM 執行、可重複執行更新版本

**Non-Goals:**
- CI/CD 全自動觸發部署(image build+push 後自動觸發 EC2 拉新版上線)——本 change 只做到「build+push 自動化」,「觸發部署」這一步仍由人手動執行 `deploy.sh`;全自動觸發是未來獨立 change(見決策9)
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

### 5. Image 來源:CI 自動 build+push 到 GHCR,EC2 端 docker pull(取代 git clone + 現場 build)

**變更緣由**(implement 中途發現、已補一輪 grill-me,見對話紀錄):task 6.1 實測時,EC2 上的 git clone/pull 卡在 GitHub fine-grained PAT 需要組織管理員核准(pending 狀態),且發現 code 當時根本還沒 push 上遠端。既然遲早要走 CI/CD,判斷不如直接把「image 來源」換成 registry-based,一次解決「EC2 端要不要處理 git 認證」這個問題,而不是修好 PAT 核准後留著一個之後還是要淘汰的機制。

**決策**:
- **Registry**:GHCR(GitHub Container Registry),不用 Docker Hub——重用 Actions 內建的 `GITHUB_TOKEN`(每次 workflow run 自動產生、run 結束即失效,不需另開帳號、不需人工核准),省掉一組第三方服務憑證的管理成本
- **觸發時機**:GitHub Actions workflow 設定 `on: push: branches: [develop]`——PR 併入 `develop` 即自動觸發 build+push,不需要人工介入這一步(僅止於 build+push,見上方 Non-Goals 的範圍界線)
- **Image 可見度**:private——`Dockerfile` 的 `COPY . .` 會把整個原始碼打進 image layer,image 本身等同原始碼,設 public 等於程式碼變相公開,跟 repo 本身是否 private 無關
- **EC2 端拉 image 認證**:GHCR 私有 image 需要登入才能 `docker pull`。使用 **classic PAT**(scope 僅 `read:packages`),不用 fine-grained PAT——classic PAT 不受 org 的「fine-grained token 需管理員核准」規則約束,建立後直接可用,不會重演 task 6.1 卡住的核准問題
- **Tag 策略**:雙 tag——`latest`(deploy.sh 預設拉這個,維持簡單/冪等)+ `sha-<git 短 hash>`(明確可回滾的版本,補上原本「僅能靠 git revert 重跑整個流程」這條回滾風險的更精確版本)
- **`docker-compose.prod.yml` 的傳遞方式**:EC2 不再需要任何原始碼或 repo,`docker-compose.prod.yml`(僅是「怎麼跑 image」的設定檔,非原始碼,體積小)改用跟決策 6 的 `.env` 相同手法——`deploy.sh` 本機執行時將內容 base64 編碼後透過 `aws ssm send-command` 直接寫入 EC2,不透過 git

**結果**:EC2 上完全不需要 git、不需要 GitHub 認證(原先的 git PAT 設定作廢,可以移除)。認證只剩兩處:本機推 code 用你自己既有的 GitHub 認證(已驗證可用)、EC2 拉 image 用上述 GHCR classic PAT。

**額外附帶效益**:原本 t2.micro 1GB RAM 在 `docker compose build` 當下的記憶體壓力風險(見下方 Risks)大幅降低——EC2 端只做 `docker pull`+`up -d`,不再於機器上執行完整 build。

### 6. Secrets:使用者手動管理,腳本不碰
`.env` 由使用者透過一次性 SSM session 手動在 EC2 上建立,固定路徑(`/opt/jiu-sync-backend/.env`,`docker-compose.prod.yml` 用 `env_file: .env` 讀取)。部署腳本執行前檢查該檔案是否存在,不存在則明確報錯中止(對應 spec 的「缺少必要環境設定時部署明確失敗」需求),不自動產生、不填預設值。

**新增**(決策5變更後):`.env` 額外併入 `GHCR_USERNAME`、`GHCR_TOKEN`(見決策5的 GHCR classic PAT)——延續同一套「secrets 集中在 `.env`,人工管理」慣例,不另開檔案。`deploy.sh` 用這兩個變數做 `docker login ghcr.io`。

### 7. 部署腳本:本機執行,SSM send-command 觸發
`infra/scripts/deploy.sh`,使用者在自己電腦手動執行,不是 Terraform provisioner、也不由 GitHub Actions 觸發(見決策5的範圍界線)。腳本邏輯(決策5變更後改寫):
1. 組出要在遠端執行的 shell script 字串:check `.env` 存在 → 將 `docker-compose.prod.yml` 內容 base64 寫入 EC2 → `docker login ghcr.io -u $GHCR_USERNAME -p $GHCR_TOKEN`(讀自 `.env`)→ `docker compose -f docker-compose.prod.yml pull` → `docker compose -f docker-compose.prod.yml up -d` → `exec app python manage.py migrate` → `exec app python manage.py collectstatic --noinput`
2. `aws ssm send-command --instance-ids <id> --document-name AWS-RunShellScript --parameters commands=[...]` 送出
3. Poll `aws ssm get-command-invocation` 直到 `Status` 為終態(`Success`/`Failed`/`TimedOut`/`Cancelled`)
4. 印出遠端執行的 stdout/stderr,`Success` 以外的狀態讓腳本以非 0 exit code 結束(可被 CI 或使用者的其他自動化偵測失敗)

不使用 SSH:instance 的 Security Group 沒開 port 22,也沒有 SSH key pair(`terraform-ec2-provision` 的既有設計),全部管理走 SSM。操作用的 IAM User 已在該 change 的 task 4.2 補齊 `ssm:SendCommand`/`ssm:GetCommandInvocation` 權限。EC2 上不再需要 git,原先設定的 git PAT 認證作廢可移除。

### 8. 冪等性
不特別設計「偵測有無變更才動作」的邏輯——`docker pull` 拉到跟本地相同 digest 的 image 時是 no-op,`docker compose up -d` 沒有 image/設定變更的 service 不會被重啟。整個流程天然冪等,重跑安全,不需要額外的版本比對機制。

### 9. CI:GitHub Actions 自動 build+push(決策5範圍內,僅此一步自動化)
新增 `.github/workflows/build-push.yml`:`on: push: branches: [develop]` 觸發,steps 為 checkout → `docker/login-action`(用內建 `GITHUB_TOKEN` 登入 GHCR,`packages: write` 權限)→ `docker/build-push-action`(沿用既有 `Dockerfile`,`build-args: ENV=prod`,tag 為 `ghcr.io/<org>/jiu-sync-backend:latest` 與 `:sha-<短 hash>` 雙 tag)。這一步全自動、不需人工介入,但**只做到 push image 為止**——觸發 EC2 部署仍是人手動執行 `deploy.sh`(見 Non-Goals)。

## Risks / Trade-offs

- **[風險] `t2.micro` 1GB RAM** → 決策5變更後風險已降低:EC2 端不再執行 `docker compose build`(記憶體壓力最大的步驟移到 GitHub Actions runner 上執行),只剩 `docker pull` + 三服務啟動,對 1GB RAM 更友善;仍維持先試跑觀察的態度,若 OOM,SSM command 會回報非 Success,不會靜默失敗
- **[風險] `sslip.io` 依賴 Elastic IP 不變** → 已於決策 4 說明,IP 變動需要重新申請憑證,這是接受的已知限制
- **[風險] GHCR classic PAT(`read:packages`)存於 EC2 的 `.env`** → 比照決策6既有的 secrets 手動管理哲學,人工建立/輪替,部署腳本只讀取不管理;PAT 若外洩,影響範圍僅限「能 pull 這個 private image」,已經是刻意縮到最小的 scope
- **[風險] 部署腳本透過 SSM 執行,單次 `send-command` 的輸出/執行時間有 AWS 內建上限**(command 輸出 truncate、預設 timeout)→ 決策5變更後這個風險同樣降低(不再跑冗長的 build),先用預設值驗證,真的卡住再調整

## Migration Plan

- **首次部署**:確認 `terraform-ec2-provision` 的 EC2 已 running、code 已 push 到 `develop`(觸發過一次 Actions build+push)、`.env` 已手動建立(含 GHCR 憑證),執行 `./infra/scripts/deploy.sh`
- **後續更新**:code 併入 `develop` 後 Actions 自動 build+push 新 image,想上線時手動重跑同一支 `deploy.sh`(拉 `latest`)
- **Rollback**:若新版本部署後發現問題,可手動將 EC2 上的 `docker-compose.prod.yml` image tag 改成先前的 `sha-<hash>`(GHCR 保留歷史 tag),重跑 `deploy.sh` 對應邏輯即可回到指定版本,比純 `git revert` 更明確;資料庫 migration 若不可逆,需要額外手動處理,不在此 change 自動化範圍內
