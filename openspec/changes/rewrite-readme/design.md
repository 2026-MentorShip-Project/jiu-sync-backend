## Context

- 讀者有兩群：評審 / 面試官（想先看懂專案在做什麼、亮點在哪）與新開發者（要能啟動與找到延伸文件）
- `docs/report/` 已有完整技術文件（架構、擴展、容錯、安全、技術債、ER、UML），資料基準日 2026-10-01
- 不可妥協邊界：無（純文件，不涉及資料寫入、併發、冪等、rollback）

## Goals / Non-Goals

**Goals:**
- 一份 README 同時服務兩群讀者：上半部給評審，下半部給開發者
- README 內的每個指令、路徑、連結都對應到 repo 實際存在的檔案

**Non-Goals:**
- 不修改 `docs/report/` 內容
- 不撰寫完整開發者手冊（環境變數逐項說明、常見問題等）
- 不處理 `CLAUDE.md` 的流程規則內容，README 只摘要並連結

## Decisions

- **D1 段落順序（兩份檔案一致）**：
  1. 標題 + 語言切換連結 + 一句話介紹
  2. 專案亮點（條列）
  3. 技術棧
  4. 系統架構摘要（一段文字 + 連到 `docs/report/01-architecture.md`）
  5. Quick Start（`.env`、`docker compose up -d`、`uv sync`、`migrate`、`runserver`、`pytest`、Celery worker 一行）
  6. 部署與可觀測性（Terraform EC2、GitHub Actions CI / build-push、nginx、Grafana / Alloy 摘要）
  7. 開發流程（OpenSpec + TDD，連結 `CLAUDE.md`、`openspec/`）
  8. Branch 規範（沿用現有規則與 branch protection 說明）
  9. 延伸文件（`docs/report/` 章節索引）
  - 理由：評審由上往下讀，前 4 段即可掌握全貌；開發者直接跳到第 5 段之後。替代方案「開發者段落優先」會讓評審第一眼看到指令，不採用。
- **D2 雙語拆檔**：`README.md` 為繁中主檔，`README.en.md` 為英文；兩份頂端第一行互相連結。GitHub 預設顯示 `README.md`，主要讀者為中文使用者。替代方案「同檔並列」會讓檔案過長，不採用。
- **D3 摘要 + 連結，不重複**：細節只存在 `docs/report/`，README 每個摘要段附連結，避免兩處內容漂移。
- **D4 不寫自動化測試**：README 是文件不是實作程式碼，不適用 TDD 規範；正確性由人工檢視（manual）。替代方案「結構測試（段落標題、雙語互連、連結有效性）」曾實作，但會把段落標題寫死、每次調整 README 都得連帶改測試，成本大於效益，已移除。

## Risks / Trade-offs

- [中英文兩份內容日後漂移] → 兩份段落結構一致，內容一致性靠人工，在 README 頂端註明以中文版為準
- [README 指令過時（例如 compose 服務名稱改變）] → Quick Start 只寫最少指令
- [docs/report/ 檔案改名造成連結失效] → 改名時人工更新 README 連結
