## Why

`terraform-ec2-provision` 已建好一台可透過 SSM 管理、對外開放 80/443 的 EC2 instance,但上面還沒有任何服務在跑。需要把 Django 後端容器化,並把它跟 nginx、TLS 一起部署上去,才能讓前端真正打到一個穩定、有 HTTPS 的正式環境,而不是每次都要靠臨時的 ngrok tunnel。

## What Changes

- 新增 `Dockerfile`,以 uv 官方建議寫法容器化 Django app,支援 dev/prod 兩種 build 模式
- 新增 `pyproject.toml` 的 `gunicorn` 依賴(WSGI server,`docker-compose.yml` 本機開發環境目前沒有,正式環境跑容器必須要有)
- 新增 `infra/docker/docker-compose.prod.yml`:app(Django/gunicorn)+ db(Postgres)+ redis 三個服務,取代本機用的 `docker-compose.yml`(本機那份保留給開發用,不動它)
- 新增 nginx 設定檔(host native 安裝,非 container):reverse proxy 到 app container,處理 TLS 終止
- 新增 TLS 自動化:透過免費萬用網域服務 `sslip.io`(對應 EC2 的 Elastic IP,不需購買網域)申請 Let's Encrypt 憑證
- 新增 `.github/workflows/build-push.yml`:push 到 `develop` 自動 build image、push 到 GHCR(private,`latest` + `sha-<hash>` 雙 tag)——**僅自動化「build+push」這一步**,見決策5
- 新增 `infra/scripts/deploy.sh`:本機執行的部署腳本,透過 AWS SSM `send-command`(非 SSH)連進 EC2,執行 `docker login` GHCR、`docker compose pull`、`up`、`migrate`、`collectstatic`,設計為可重複執行(冪等)。EC2 端不再需要 git、不再 clone 原始碼——`docker-compose.prod.yml` 也改由此腳本透過 SSM 直接寫入(implement 中途發現 git PAT 需組織核准卡住部署,已補一輪 grill-me 改為此設計,詳見 design.md 決策5)
- 不包含:CI/CD 全自動觸發部署(build+push 後自動觸發 EC2 上線,見 design.md 決策9)、正式網域購買與 DNS 切換、secrets 自動化管理(Parameter Store/Secrets Manager)——這些留給未來的 change

## Capabilities

### New Capabilities
- `infra/app-deployment`:把 Django 後端容器化並部署到既有 EC2 instance 上,含 nginx reverse proxy、TLS、可重複執行的部署流程

### Modified Capabilities

(無 — 此 change 不修改任何既有 capability 的需求;`infra/ec2-provisioning` 的資源本身不變,只是在其上疊加應用層部署)

## Impact

- 新增檔案:`Dockerfile`、`infra/docker/docker-compose.prod.yml`、`.github/workflows/build-push.yml`、`infra/scripts/deploy.sh`(nginx 設定檔為 host native 直接於 EC2 上設定,不進 repo)
- 修改檔案:`pyproject.toml`(新增 `gunicorn` 依賴)
- 依賴 `terraform-ec2-provision` 這個 change 已建立的資源(instance、SSM 存取、Security Group),此 change 不會修改任何 `.tf` 檔案
- 不影響現有 API 行為、不影響本機開發流程(`docker-compose.yml` 保持不變)
- 產生新的對外可存取端點:`https://<EIP>.sslip.io`(真實 HTTPS,Let's Encrypt 憑證)
- 新增外部依賴:GHCR(GitHub Container Registry,private image)——需要一組 classic PAT(`read:packages`)供 EC2 端 `docker login` 使用
