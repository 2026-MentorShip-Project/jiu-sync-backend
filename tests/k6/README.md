## 投票容量測試

從自己的電腦執行 k6，對專用測試活動送出流量。每次測試最多每位使用者建立一筆投票，不呼叫 AI、不帶 email；請勿使用真實活動。兩次 40 人測試共新增約 80 筆投票，會保留在活動中供核對。

先執行持續瀏覽與投票測試（逐步升到 40 人，再維持 10 分鐘）：

```sh
k6 run -e BASE_URL=https://your-backend.example -e EVENT_ID=TEST_EVENT_ID \
  -e ALLOW_TEST_VOTES=1 -e MODE=steady \
  --summary-export=/tmp/voting-steady.json tests/k6/voting-load.js
```

再執行 40 人集中提交：

```sh
k6 run -e BASE_URL=https://your-backend.example -e EVENT_ID=TEST_EVENT_ID \
  -e ALLOW_TEST_VOTES=1 -e MODE=burst \
  --summary-export=/tmp/voting-burst.json tests/k6/voting-load.js
```

`VUS` 預設 40，上限 60；`HOLD_DURATION` 可調整 steady 的持續時間。burst 直接同時 POST；steady 先讀詳情、停留 2–5 秒再投票，之後每約 5 秒讀詳情及 poll。這是保守的讀取流量模型，不宣稱與前端實際輪詢頻率完全一致。

建議通過條件：所有投票 201 並實際存入一次、沒有 5xx／網路失敗、整體請求失敗率低於 1%、投票 p95 小於 3 秒、詳情與輪詢 p95 小於 1 秒。腳本會在錯誤超標後停止，避免持續施壓；使用 Ctrl+C 可手動停止。

同時在 EC2／Grafana 觀察 CPU、CPUCreditBalance、記憶體、容器重啟與 PgBouncer 等待情形。伺服器另開終端執行 `docker stats`。本機通過只能驗證腳本與本機行為，不能代替 t2.micro 的實際容量測試。

每個 HTTP 請求最多等 20 秒。POST 逾時時不自動重試，因為伺服器可能仍會完成寫入；停止負載後需再次 GET 活動，依 console 記錄的 nickname prefix 核對資料。客戶端逾時不能直接推論資料遺失。即使稍後全部存入，集中投票延遲仍可能不符合使用體驗。

正式機實測結果見 [投票正式機壓測報告](../../docs/voting-load-test-production.md)。這是 API 壓測，不涵蓋前端渲染、登入、修改投票、寄信或 AI 推薦。
