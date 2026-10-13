## Why

現有 `README.md` 仍停留在專案骨架階段（例如寫著「空 app 骨架，待 openspec change 填入」），沒有反映目前已上線的功能、部署架構與可觀測性。評審看不出專案亮點，新加入的開發者也找不到正確的入口與延伸文件。

## What Changes

- 重寫 `README.md`（繁體中文，主檔）：專案簡介與亮點、技術棧、精簡 Quick Start、部署與可觀測性摘要、開發流程（OpenSpec / TDD）與 Branch 規範
- 新增 `README.en.md`（英文版），內容與中文主檔段落對應；兩份檔案頂端互相連結
- 架構、ER、API、安全性等細節不重複撰寫，改以連結指向 `docs/report/`

## Capabilities

### New Capabilities

（無）

### Modified Capabilities

（無）— 純文件變更，不影響任何系統行為，已在 change 設定檔設定 skip_specs: true

## Impact

- 檔案：`README.md`（改寫）、`README.en.md`（新增）
- 不影響程式碼、API、相依套件與部署流程
