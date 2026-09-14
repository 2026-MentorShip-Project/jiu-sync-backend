## 1. 環境健檢

- [x] 1.1 滿足需求「對外請求由 Django 後端實際處理」的前置條件:確認本機與遠端環境就緒——本機 `docker --version`/`docker compose version` 顯示已安裝可用;`aws ssm describe-instance-information --filters "Key=InstanceIds,Values=i-0f6d5dc974e91bbf6"` 確認 EC2 仍為 `PingStatus: Online`(距上次驗證已過一段時間,需重新確認);`git status` 顯示在正確 branch、無非預期未追蹤檔案;確認 `pyproject.toml` 目前確實沒有 `gunicorn`(待後續 task 補上)。(auto)

## 2. 容器化 Django App

- [x] 2.1 滿足需求「對外請求由 Django 後端實際處理」:於 `pyproject.toml` 新增 `gunicorn` 至 `dependencies`;建立根目錄 `Dockerfile`(採用既定範本,`CMD` 使用 `config.wsgi:application`)。驗證:本機 `docker build --build-arg ENV=prod -t jiu-sync-backend:test .` 成功;`docker run --rm jiu-sync-backend:test gunicorn --version` 確認 gunicorn 於 image 內可執行;`uv lock` 更新後的 lock file 與 `pyproject.toml` 一致(`uv sync --locked` 不報錯)。(auto)

## 3. 正式環境 docker compose 組態

- [x] 3.1 滿足需求「應用程式 port 不直接對外暴露」:建立 `infra/docker/docker-compose.prod.yml`(app + db + redis 三服務,app 使用 task 2.1 的 Dockerfile build,`env_file: .env`,port mapping 僅 `127.0.0.1:8000:8000`,不綁 `0.0.0.0`)。驗證:本機以測試用假 `.env` 執行 `docker compose -f infra/docker/docker-compose.prod.yml config` 確認語法正確;`docker compose -f infra/docker/docker-compose.prod.yml build` 成功;可選以本機假環境變數 `up` 一次確認三服務都能啟動不 crash-loop(db/redis 連線成功)。(auto)

## 4. nginx + TLS(於既有 EC2 上一次性設定)

- [x] 4.1 滿足需求「對外服務透過瀏覽器信任的 HTTPS 提供」:透過 SSM send-command 於 EC2 上安裝 `nginx`、`certbot`、`python3-certbot-nginx`;寫入 nginx server block(`server_name 56-155-65-244.sslip.io`,reverse proxy 至 `127.0.0.1:8000`,80 導向 443);執行 `certbot --nginx -d 56-155-65-244.sslip.io --non-interactive --agree-tos -m <email>` 申請憑證。驗證:本機 `curl -v https://56-155-65-244.sslip.io` 確認 TLS handshake 成功且憑證受信任(無 `-k`/`--insecure` 也能連);此階段 app 尚未部署,回應 502/504 視為正常(僅驗證 HTTPS 層,不驗證應用層回應,那是 task 6.1 的範圍)。(auto)

## 5. 部署腳本

- [x] 5.1 滿足需求「缺少必要環境設定時部署明確失敗」:建立 `infra/scripts/deploy.sh`,腳本組出的遠端 shell 邏輯第一步先檢查 `/opt/jiu-sync-backend/.env` 是否存在,不存在則明確印出錯誤訊息並以非 0 exit code 中止,不繼續往下執行 git/docker 相關步驟。驗證:在 EC2 上 `.env` 尚未建立的狀態下執行 `./infra/scripts/deploy.sh`,確認腳本回報明確錯誤並中止(邊界案例優先驗證,而非等有 `.env` 才測)。(auto)
- [x] 5.2 滿足需求「部署動作可重複執行且會套用最新程式碼」:補完 `deploy.sh` 其餘邏輯(`.env` 存在時:git clone-or-pull → `docker compose -f infra/docker/docker-compose.prod.yml build` → `up -d` → `exec app python manage.py migrate` → `exec app python manage.py collectstatic --noinput`),透過 `aws ssm send-command` 執行、poll `get-command-invocation` 至終態、依 `Status` 決定腳本自身 exit code。驗證:`terraform validate`-等級的語法檢查(`bash -n infra/scripts/deploy.sh`)通過;此 task 先不要求成功完整跑過一次(需要 task 6.1 的手動前置設定才能真的執行到底),但腳本邏輯需完整可讀、無語法錯誤。(auto)

## 6. 首次端到端部署與驗證(manual 前置設定 + auto 驗證)

- [ ] 6.1 滿足需求「對外請求由 Django 後端實際處理」:人工前置設定(不可由 agent 自動化,需使用者本人操作)——透過一次性 SSM session 在 EC2 的 `/opt/jiu-sync-backend/` 建立正式 `.env`,並設定好 git 認證(例如 fine-grained GitHub PAT 寫入 `~/.git-credentials`)。設定完成後執行 `./infra/scripts/deploy.sh`。驗證:`curl https://56-155-65-244.sslip.io/api/me/` 回傳本專案定義的 401 格式(`{"message": ..., "code": ...}`),確認是 Django 而非 nginx 502/默認頁在回應。(manual 前置 + auto 驗證)

## 7. 冪等性與可重複部署驗證

- [ ] 7.1 滿足需求「部署動作可重複執行且會套用最新程式碼」:修改一行後端 code(引入一個可從外部觀察的變化,例如在既有端點回應中加入可辨識的版本標記),commit 後重跑 `deploy.sh`,驗證新版本確實生效(`curl` 觀察到變化)。接著在無新 commit 的狀態下再跑一次 `deploy.sh`,驗證服務仍正常回應、無錯誤發生(對應 spec 的兩個情境:有變更會更新、無變更不出錯)。(auto)

## 8. 網路暴露面驗證

- [ ] 8.1 滿足需求「應用程式 port 不直接對外暴露」:透過 SSM send-command 在 EC2 上執行 `ss -tlnp | grep 8000`(或 `docker port <app_container>`),確認 app container 的 port mapping 確實只綁定在 `127.0.0.1:8000`、不是 `0.0.0.0:8000`——這是防禦性的實質驗證(即使 Security Group 之後被誤改開放 8000,loopback binding 仍能擋下外部直連)。額外從 instance 網路之外對其公有 IP 執行 `nc -zv -w5 56.155.65.244 8000` 作為輔助佐證,確認目前 Security Group 層也一致擋住(此測試在 SG 層與 `terraform-ec2-provision` 的既有驗證重疊,僅作補充,不是本 task 的主要證據)。(auto)
