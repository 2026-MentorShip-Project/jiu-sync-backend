# nginx 設定(EC2 host)

nginx 直接裝在 EC2 host 上，沒有放進 docker compose（原因見 `openspec/changes/deploy-django-app/design.md` Decision 3），負責 TLS 終止和 reverse proxy，是唯一對外開放的服務。

**這個目錄裡的設定檔只是參考副本。** `deploy.sh` 不會傳送 nginx 設定，EC2 上的檔案也不會跟著 repo 自動同步。在 EC2 上改了設定之後，要手動更新這裡的副本，反過來也一樣。

| 項目 | 位置 |
|---|---|
| 實際生效的設定 | EC2 `/etc/nginx/sites-available/jiu-sync-backend`(`sites-enabled/` 底下有 symlink 指向它) |
| repo 參考副本 | `infra/nginx/jiu-sync-backend.conf`(2026-10-01 從 EC2 取得) |
| 加入 `/metrics` 規則前的備份 | EC2 `/var/backups/nginx-jiu-sync-backend-20261001` |
| TLS 憑證 | `/etc/letsencrypt/live/56-155-65-244.sslip.io/`(certbot 管理,systemd timer 自動續期) |

## 請求流程

```
使用者 ──https:443──> nginx ──http──> 127.0.0.1:8000 ──> app container(gunicorn)
使用者 ──http:80───> nginx ──301──> https://...
Alloy  ──http──> app:8000/metrics      (compose 內網,不經過 nginx)
```

app container 的 port 只綁在 loopback(`127.0.0.1:8000:8000`)，外部無法直接連到。

## 設定說明

### 443 server（主要）

| 設定 | 作用 |
|---|---|
| `server_name 56-155-65-244.sslip.io` | 對外網域(sslip.io 把網域對應到 EIP `56.155.65.244`) |
| `location = /metrics { return 404; }` | 對外封鎖 metrics 端點。這是 `add-observability-stack` design.md D3 的第二道防線,主要防線是 app 端的 `METRICS_TOKEN` 檢查。`=` 表示完全比對,不影響其他路徑 |
| `location /` → `proxy_pass http://127.0.0.1:8000` | 其他所有請求都轉給 app |
| `proxy_set_header Host $host` | 把原本的網域傳給 Django,讓 `ALLOWED_HOSTS` 檢查 |
| `X-Real-IP` / `X-Forwarded-For` | 傳遞使用者的真實 IP(rate limit 和 log 會用到) |
| `X-Forwarded-Proto $scheme` | 告訴 Django 原始請求是 https。prod settings 的 `SECURE_PROXY_SSL_HEADER` 依這個 header 判斷,沒有它的話,`SECURE_SSL_REDIRECT` 會讓請求無限轉址 |
| `listen 443 ssl` 和 `ssl_*` | 由 certbot 產生,不要手動修改 |

### 80 server（轉址）

certbot 產生的區塊:只要網域相符就 301 轉到 https,其他 Host 一律回 404。

## 已知行為

- **對外的 `/metrics` 回的是 nginx 的 HTML 404**(內容有 `<center>nginx/...</center>`),請求不會進到 Django。驗證時要看回應內容,不能只看狀態碼。
- **nginx 會把使用者送來的 `Host` 原樣轉給 Django。** 443 只有一個 server,所以它也是預設 server,任何 Host 都會被它接住。2026-10-01 實測 `-H "Host: app"` 打 `/healthz/` 得到 400,是 Django 的 `ALLOWED_HOSTS` 擋下的。
  - task 2.3 上線後,`app` 會加進 `ALLOWED_HOSTS`,這種請求會被放行。但它經過 nginx,一定帶著 `X-Forwarded-Proto: https`,所以照常走 https 流程。`/metrics` 本身仍然會先被 nginx 擋下。這點在 D3 已經評估並接受。
  - 如果之後想在 nginx 這層直接拒絕未知的 Host,可以加一個 `default_server` 搭配 `return 444`。目前沒有這樣做。

## 修改設定的步驟

用 SSM 連進 EC2 後:

```bash
# 1. 先備份。不要放在 sites-enabled/ 或 sites-available/ 裡,
#    sites-enabled/ 底下的檔案都會被 nginx 載入
sudo cp /etc/nginx/sites-available/jiu-sync-backend /var/backups/nginx-jiu-sync-backend-$(date +%Y%m%d)

# 2. 編輯
sudo nano /etc/nginx/sites-available/jiu-sync-backend

# 3. 先檢查語法,通過才 reload。語法錯誤時 nginx 會繼續使用舊設定
sudo nginx -t && sudo systemctl reload nginx
```

改完後,記得同步更新 repo 裡的 `infra/nginx/jiu-sync-backend.conf`。

### 回滾

```bash
sudo cp /var/backups/nginx-jiu-sync-backend-<日期> /etc/nginx/sites-available/jiu-sync-backend
sudo nginx -t && sudo systemctl reload nginx
```

### 驗證(在本機執行)

```bash
curl -s https://56-155-65-244.sslip.io/metrics | head -5                       # 預期:nginx 的 HTML 404
curl -s -o /dev/null -w "%{http_code}\n" https://56-155-65-244.sslip.io/healthz/  # 預期:200
curl -s -o /dev/null -w "%{http_code}\n" http://56-155-65-244.sslip.io/healthz/   # 預期:301
```

## 注意事項

- **certbot 可能會改寫這個檔案**:執行 `certbot --nginx` 時會修改 `# managed by Certbot` 的那幾行。自動續期只會更新憑證、不會改設定檔,但如果重新執行 `certbot --nginx`,事後要重新比對 repo 裡的副本。
- **換機器或重建 EC2 時**,nginx 設定不會自動還原,要參考這份副本手動重建,`/metrics` 那一行一定要加回去。
