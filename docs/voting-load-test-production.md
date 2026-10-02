## 正式機投票壓測（2026-10-02 至 2026-10-03，台灣時間）

目標：`https://56-155-65-244.sslip.io`，EC2 `t2.micro`。活動為使用者提供的 `0uIApQmd`（壓測專用10/2），7 個候選時段，開始時 0 筆投票。

測試從使用者的電腦執行 k6，直接打正式後端 API。虛擬使用者不是實際瀏覽器；不涵蓋前端渲染、登入、修改投票、寄信或 AI 推薦。後續透過 SSM 唯讀核對部署與紀錄；未取得壓測當下的記憶體、swap 讀寫及資料庫等待數據。

| 情境 | 操作 | 結果 |
| --- | --- | --- |
| 40 人持續在線 | 依序升至 10、20、40 人；40 人維持 10 分鐘。每人看活動、停留 2–5 秒、投一票，之後約每 5 秒讀詳情與 poll | 9962 個 HTTP 請求，0 失敗；40/40 筆投票存入 |
| 40 人集中送出 | 40 個 VU 各直接 POST 一筆投票 | 40/40 個 POST 超過 20 秒客戶端等待上限；錯誤門檻觸發而停止 |

持續負載於 10/2 23:51:18 開始、10/3 00:03:07 完成。詳情 p95 142.72ms、poll p95 66.05ms、投票平均 1.94 秒、p95 3.38 秒、最慢 3.45 秒。投票 p95 超過原訂 3 秒門檻，所以 k6 exit code 為 99；請求與資料正確性檢查均通過。

集中投票於 10/3 00:03:46 開始，約 00:04:08 停止。40 個 POST 都逾時，沒有收到 201；整輪已記錄 55 個請求，其中 40 個為失敗 POST（72.73%）。部分輪詢迭代在停止時未完成，故不能將該比例解讀成「40 人投票失敗率只有 72.73%」。投票 p95 約 20 秒是客戶端逾時截斷值，並非實際服務完成時間。

集中測試結束當下即時核對為 0/40，之後回查先看到 35/40、最終看到 40/40；各筆暱稱唯一、7 個時段資料完整。活動最終共 80 筆測試投票。停止負載後 healthz 回應 HTTP 200，約 158ms。結果支持「集中提交處理太慢，逾時後仍可能寫入」，不支持把逾時視為資料遺失，也不足以斷言特定 CPU／DB／worker 瓶頸。

CloudWatch 持續負載段觀察：23:55 的 5 分鐘 CPU 平均約 22.83%；00:00 時段當時可見的平均約 19.58%，可能尚未完整匯入。CPUCreditBalance 由 23:50 的 47.67 降至 00:00 的 46.00，未耗盡。這些資料未涵蓋完整集中送出段，不能用來排除短暫 CPU 飽和或其他瓶頸。

結論：此輪 40 人分散投票與持續瀏覽沒有請求失敗，但投票 p95 略超門檻；40 人幾乎同時送出則有嚴重延遲，不能宣稱已通過集中投票容量測試。應避免把「40 人在線」和「40 個 POST 同時到達」視為同一種負載。

## 後續唯讀核對

10/3 00:15:15 讀取正式機狀態：Gunicorn 為 gthread、3 workers × 8 threads、timeout 60 秒，啟動參數沒有另外覆寫 workers／threads。app 容器自 10/2 19:28 啟動，RestartCount=0、OOMKilled=false；指定壓測時段的 app logs 未找到 worker timeout 或 worker 重啟訊息。

Nginx access log 在 00:04:06 記錄 40 筆集中投票的 HTTP 499，符合客戶端 20 秒逾時後斷線。既有 log 沒有 request_time／upstream_response_time，不能從這份紀錄拆出排隊與應用處理耗時。

事後主機 snapshot 約 209MiB available、481MiB swap used；這不是壓測當下的峰值，swap 已使用量也不代表當時正在換頁。尚無足夠分段計時資料證明瓶頸在 CPU、密碼雜湊、資料庫或 worker 排隊。

## 重跑

每次新建測試暱稱，不自動重試 POST。建議新建專用活動以保持每輪起始投票數一致；同一活動重跑會繼續累積測試投票，影響詳情回應大小。

```sh
k6 run --quiet -e BASE_URL=https://56-155-65-244.sslip.io \
  -e EVENT_ID=0uIApQmd -e ALLOW_TEST_VOTES=1 -e MODE=steady -e VUS=40 \
  -e HOLD_DURATION=10m --summary-export=/tmp/voting-steady-repeat.json \
  tests/k6/voting-load.js

k6 run --quiet -e BASE_URL=https://56-155-65-244.sslip.io \
  -e EVENT_ID=0uIApQmd -e ALLOW_TEST_VOTES=1 -e MODE=burst -e VUS=40 \
  --summary-export=/tmp/voting-burst-repeat.json tests/k6/voting-load.js
```

本次原始摘要：`/private/tmp/voting-production-steady.json`、`/private/tmp/voting-production-burst.json`。
