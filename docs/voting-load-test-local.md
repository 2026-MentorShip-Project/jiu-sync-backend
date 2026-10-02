## 本機壓測驗證（2026-10-02）

這是本機 macOS 結果，不能代表正式 t2.micro 的容量。使用 Gunicorn gthread 3 workers × 8 threads、Django dev 設定、本機 PostgreSQL／PgBouncer，專用活動有 3 個候選時段。

| 情境 | 投票數 | HTTP 請求數 | 失敗率 | 投票 p95 | 詳情 p95 | 輪詢 p95 |
| --- | --- | --- | --- | --- | --- | --- |
| 40 人集中提交 | 40/40 | 82 | 0% | 976.42ms | — | 306.93ms |
| 逐步增加到 40 人（短版） | 40/40 | 1198 | 0% | 164.05ms | 23.33ms | 10.22ms |

短版依序 30 秒增加到 10、20、40 人，維持 40 人 30 秒後降低至 0，含結尾完成請求共約 2 分 18 秒。兩次均核對每位使用者的投票恰好一筆，且所有候選時段資料存在。

本機專用活動 ID：`irF7IJpv`；共保留 80 筆測試投票。摘要 JSON 位於 `/private/tmp/voting-local-burst.json` 與 `/private/tmp/voting-local-steady.json`。

前端 `https://that-is-so-sweet.vercel.app/` 的公開部署檔案指向 `https://56-155-65-244.sslip.io`；僅驗證該後端 healthz 回應 200，尚未對正式後端執行負載測試。需要正式環境的專用活動分享連結後，才能執行並判斷正式機容量。
