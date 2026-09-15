## 1. 環境健檢

- [x] 1.1 滿足需求「對外請求由 Django 後端實際處理」的前置條件:確認本機與遠端環境就緒——本機 `docker --version`/`docker compose version` 顯示已安裝可用;`aws ssm describe-instance-information --filters "Key=InstanceIds,Values=i-0f6d5dc974e91bbf6"` 確認 EC2 仍為 `PingStatus: Online`(距上次驗證已過一段時間,需重新確認);`git status` 顯示在正確 branch、無非預期未追蹤檔案;確認 `pyproject.toml` 目前確實沒有 `gunicorn`(待後續 task 補上)。(auto)

## 2. 容器化 Django App

- [x] 2.1 滿足需求「對外請求由 Django 後端實際處理」:於 `pyproject.toml` 新增 `gunicorn` 至 `dependencies`;建立根目錄 `Dockerfile`(採用既定範本,`CMD` 使用 `config.wsgi:application`)。驗證:本機 `docker build --build-arg ENV=prod -t jiu-sync-backend:test .` 成功;`docker run --rm jiu-sync-backend:test gunicorn --version` 確認 gunicorn 於 image 內可執行;`uv lock` 更新後的 lock file 與 `pyproject.toml` 一致(`uv sync --locked` 不報錯)。(auto)

## 3. 正式環境 docker compose 組態

> **註**:本節 3.1 建立當下設計為 git-clone + 現場 build(`build:`)。task 6.1 實測時發現 git 認證卡住,已補一輪 grill-me 改為 CI build+push + registry pull,`app` service 改為 `image:`,見決策5與下方第 10 節(task 10.1 取代此檔案的 build 設定)。3.1 的 build/config 驗證本身仍是有效的歷史紀錄(Dockerfile 可 build 成功這件事沒變),不必重做。

- [x] 3.1 滿足需求「應用程式 port 不直接對外暴露」:建立 `infra/docker/docker-compose.prod.yml`(app + db + redis 三服務,app 使用 task 2.1 的 Dockerfile build,`env_file: .env`,port mapping 僅 `127.0.0.1:8000:8000`,不綁 `0.0.0.0`)。驗證:本機以測試用假 `.env` 執行 `docker compose -f infra/docker/docker-compose.prod.yml config` 確認語法正確;`docker compose -f infra/docker/docker-compose.prod.yml build` 成功;可選以本機假環境變數 `up` 一次確認三服務都能啟動不 crash-loop(db/redis 連線成功)。(auto)

## 4. nginx + TLS(於既有 EC2 上一次性設定)

- [x] 4.1 滿足需求「對外服務透過瀏覽器信任的 HTTPS 提供」:透過 SSM send-command 於 EC2 上安裝 `nginx`、`certbot`、`python3-certbot-nginx`;寫入 nginx server block(`server_name 56-155-65-244.sslip.io`,reverse proxy 至 `127.0.0.1:8000`,80 導向 443);執行 `certbot --nginx -d 56-155-65-244.sslip.io --non-interactive --agree-tos -m <email>` 申請憑證。驗證:本機 `curl -v https://56-155-65-244.sslip.io` 確認 TLS handshake 成功且憑證受信任(無 `-k`/`--insecure` 也能連);此階段 app 尚未部署,回應 502/504 視為正常(僅驗證 HTTPS 層,不驗證應用層回應,那是 task 6.1 的範圍)。(auto)

## 5. 部署腳本

> **註**:5.1 的 fail-fast 邏輯(檢查 `.env` 存在)在決策5變更後不變,仍有效。5.2 補的 git clone-or-pull + build 邏輯已由第 10 節(task 10.2)取代為 docker pull-based 版本,不必重做語法檢查以外的部分。

- [x] 5.1 滿足需求「缺少必要環境設定時部署明確失敗」:建立 `infra/scripts/deploy.sh`,腳本組出的遠端 shell 邏輯第一步先檢查 `/opt/jiu-sync-backend/.env` 是否存在,不存在則明確印出錯誤訊息並以非 0 exit code 中止,不繼續往下執行 git/docker 相關步驟。驗證:在 EC2 上 `.env` 尚未建立的狀態下執行 `./infra/scripts/deploy.sh`,確認腳本回報明確錯誤並中止(邊界案例優先驗證,而非等有 `.env` 才測)。(auto)
- [x] 5.2 滿足需求「部署動作可重複執行且會套用最新程式碼」:補完 `deploy.sh` 其餘邏輯(`.env` 存在時:git clone-or-pull → `docker compose -f infra/docker/docker-compose.prod.yml build` → `up -d` → `exec app python manage.py migrate` → `exec app python manage.py collectstatic --noinput`),透過 `aws ssm send-command` 執行、poll `get-command-invocation` 至終態、依 `Status` 決定腳本自身 exit code。驗證:`terraform validate`-等級的語法檢查(`bash -n infra/scripts/deploy.sh`)通過;此 task 先不要求成功完整跑過一次(需要 task 6.1 的手動前置設定才能真的執行到底),但腳本邏輯需完整可讀、無語法錯誤。(auto)

## 6. 首次端到端部署與驗證(manual 前置設定 + auto 驗證)【已由第 10 節取代,見下方 task 10.3】

- [ ] ~~6.1~~ **(superseded)** 原設計走 git clone + PAT 認證,task 6.1 實測時卡在 GitHub fine-grained PAT 需組織管理員核准,已補一輪 grill-me 改為 CI build+push + GHCR registry pull(決策5)。此 task 不再執行,驗收標準原封不動轉移到下方 **task 10.3**(同樣驗證 `curl .../api/me/` 回傳 401 JSON)。保留此行僅作歷史紀錄,不要重新嘗試 git 認證路線。

## 7. 冪等性與可重複部署驗證

- [x] 7.1 滿足需求「部署動作可重複執行且會套用最新程式碼」:修改一行後端 code(引入一個可從外部觀察的變化,例如在既有端點回應中加入可辨識的版本標記),commit 後重跑 `deploy.sh`,驗證新版本確實生效(`curl` 觀察到變化)。接著在無新 commit 的狀態下再跑一次 `deploy.sh`,驗證服務仍正常回應、無錯誤發生(對應 spec 的兩個情境:有變更會更新、無變更不出錯)。(auto)

## 8. 網路暴露面驗證

- [x] 8.1 滿足需求「應用程式 port 不直接對外暴露」:透過 SSM send-command 在 EC2 上執行 `ss -tlnp | grep 8000`(或 `docker port <app_container>`),確認 app container 的 port mapping 確實只綁定在 `127.0.0.1:8000`、不是 `0.0.0.0:8000`——這是防禦性的實質驗證(即使 Security Group 之後被誤改開放 8000,loopback binding 仍能擋下外部直連)。額外從 instance 網路之外對其公有 IP 執行 `nc -zv -w5 56.155.65.244 8000` 作為輔助佐證,確認目前 Security Group 層也一致擋住(此測試在 SG 層與 `terraform-ec2-provision` 的既有驗證重疊,僅作補充,不是本 task 的主要證據)。(auto)

## 9. CI:GitHub Actions 自動 build+push image 到 GHCR(決策5新增)

- [x] 9.1 滿足需求「部署動作可重複執行且會套用最新程式碼」的前置條件(image 來源):新增 `.github/workflows/build-push.yml`,`on: push: branches: [develop]` 觸發。Steps:checkout → 登入 GHCR(`docker/login-action`,`registry: ghcr.io`、`username: ${{ github.actor }}`、`password: ${{ secrets.GITHUB_TOKEN }}`)→ build+push(`docker/build-push-action`,context 為根目錄既有 `Dockerfile`,`build-args: ENV=prod`,tags 為 `ghcr.io/<org>/jiu-sync-backend:latest` 與 `ghcr.io/<org>/jiu-sync-backend:sha-<短 commit hash>`)。需明確確認 image visibility 為 **private**(GHCR 預設可能繼承 repo 可視性,但需要實測驗證,不可假設)。驗證:push 一個 commit 到 `develop` 觸發 workflow,以 `gh run watch` 或等效方式確認 run 結束狀態為 success;以 `gh api` 或 GHCR package 頁面確認 image 已出現、兩個 tag 皆存在、visibility 為 private。(auto)

## 10. Image-based 部署重構(決策5新增,取代第 3/5/6 節的 git-clone 版本)

- [x] 10.1 滿足需求「應用程式 port 不直接對外暴露」:修改 `infra/docker/docker-compose.prod.yml` 的 `app` service,將 `build:` 改為 `image: ghcr.io/<org>/jiu-sync-backend:latest`,其餘設定(port mapping 僅 `127.0.0.1:8000:8000`、`depends_on`、`restart: unless-stopped`)不變。驗證:本機 `docker compose -f infra/docker/docker-compose.prod.yml --env-file <test> config` 語法正確;`docker login ghcr.io` 後 `docker compose -f infra/docker/docker-compose.prod.yml pull` 成功拉到 task 9.1 產生的 image。(auto)
- [x] 10.2 滿足需求「缺少必要環境設定時部署明確失敗」與「部署動作可重複執行且會套用最新程式碼」:重寫 `infra/scripts/deploy.sh`——移除所有 git 相關邏輯(`git init`/`fetch`/`checkout` 等),改為:check `/opt/jiu-sync-backend/.env` 存在(fail-fast 邏輯不變,沿用 5.1 已驗證的部分)→ 將 `infra/docker/docker-compose.prod.yml` 內容 base64 編碼透過 SSM command 直接寫入 EC2 → `docker login ghcr.io -u $GHCR_USERNAME -p $GHCR_TOKEN`(讀自 EC2 上 `.env` 的兩個新變數)→ `docker compose pull` → `up -d` → `exec -T app python manage.py migrate` → `exec -T app python manage.py collectstatic --noinput`。驗證:`bash -n infra/scripts/deploy.sh` 語法檢查通過;在 EC2 上 `.env` 不存在時重新驗證 fail-fast(與原 5.1 標準相同);`.env` 存在但缺 `GHCR_USERNAME`/`GHCR_TOKEN` 時,`docker login` 步驟應明確失敗回報,不可靜默略過繼續往下執行。(auto)
- [x] 10.3 滿足需求「對外請求由 Django 後端實際處理」:人工前置設定(不可由 agent 自動化,需使用者本人操作)——於 GitHub 建立 classic PAT(scope 僅 `read:packages`),透過一次性 SSM session 補進 EC2 `/opt/jiu-sync-backend/.env`(新增 `GHCR_USERNAME`、`GHCR_TOKEN` 兩行,`.env` 其餘內容已於先前建立完成不需重建)。設定完成後執行 `./infra/scripts/deploy.sh`。驗證:`curl https://56-155-65-244.sslip.io/api/me/` 回傳本專案定義的 401 格式(`{"message": ..., "code": ...}`),確認是 Django 而非 nginx 502/默認頁在回應——此為原 task 6.1 的驗收標準,轉移至此。(manual 前置 + auto 驗證)
