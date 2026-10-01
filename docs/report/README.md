# 揪甘心後端技術架構文件

這組文件說明 `jiu-sync-backend` 目前的架構、設計取捨與已知限制。

如果目標是繳交或轉成 PDF，建議直接使用 [technical-report.md](technical-report.md)。這份主報告以產品情境為主軸，包含 Functional Requirements、Non-functional Requirements，以及題目要求的六個技術主題；其餘分章文件保留較完整的實作細節，可作為附錄或答辯資料。

| # | 文件 | 內容 |
| --- | --- | --- |
| 主文 | [technical-report.md](technical-report.md) | 適合直接轉成 PDF 的精簡技術報告 |
| 1 | [01-architecture.md](01-architecture.md) | 應用程式架構圖:執行期架構、建置與部署流程(CI/CD)、Terraform IaC、可觀測性系統的位置 |
| 2 | [02-ux.md](02-ux.md) | 介面/使用者體驗設計(後端角度):關鍵流程與 API 設計考量 |
| 3 | [03-scalability.md](03-scalability.md) | 擴展設計:預期規模、已做的效能設計、效能測試現況、擴展路線圖 |
| 4 | [04-fault-tolerance.md](04-fault-tolerance.md) | 容錯性設計:外部服務故障、逾時、錯誤處理、資料一致性 |
| 5 | [05-security.md](05-security.md) | 安全性設計:登入與 JWT、權限、資料保護、尚未處理的風險 |
| 6 | [06-tech-debt.md](06-tech-debt.md) | 技術債處理 Log:架構取捨、後續開發必須修改的地方、已處理紀錄 |
| 7 | [07-er-diagram.md](07-er-diagram.md) | 資料模型:ER 圖、資料表說明、約束與索引、刪除行為 |
| 8 | [08-uml-endpoints.md](08-uml-endpoints.md) | UML:主揪與參與者流程對應的 API、活動生命週期狀態機、API 一覽 |

**資料基準日:2026-10-01。** 內容依據當天 `develop` 分支(含 `feature/observability-grafana`)的程式碼、`openspec/changes/*` 的設計文件與 `docs/agents/incident-log/`。程式碼改動後,請同步更新對應章節。

## 一句話認識這個專案

揪甘心是揪團排時間的服務:**主揪**用 Google 帳號登入、建立活動與候選時段,把分享連結傳給朋友;**參與者**不用登入,用暱稱加手機末三碼投票、改票、留言。主揪可以定案、取消或重新開放投票,定案後還能請 AI 推薦聚餐餐廳。

後端是 Django REST Framework,和 PostgreSQL、PgBouncer、Redis、Celery worker、Grafana Alloy 一起以 docker compose 跑在一台 AWS EC2 上。
