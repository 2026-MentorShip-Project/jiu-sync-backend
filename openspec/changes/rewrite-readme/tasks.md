> 無新工具鏈或依賴，不需環境健檢 task。範圍小，切成 2 個垂直 task：先完成繁中主檔（可單獨上線），再補英文版與雙向連結。

## 1. 繁中主檔 README.md

- [x] 1.1 [RED→GREEN]（design.md D1、D3、D4）先在 `config/tests/test_readme.py` 寫測試：`README.md` 存在；包含 D1 九個段落的中文標題；所有相對連結指向的路徑存在（含連到不存在路徑時測試會失敗的負向案例：以暫存 Markdown 字串驗證連結檢查器能抓出壞連結、略過 `http(s)://` 與 `#anchor`）；Quick Start 引用的 `.env.example`、`docker-compose.yml` 存在。確認對現有 README 為 FAIL 後，依 D1 順序改寫 `README.md`（內容依據 `docs/report/README.md`、`pyproject.toml`、`infra/`、`.github/workflows/`，指令需對照實際檔案）— (auto) `uv run pytest config/tests/test_readme.py -q`、`uv run pytest -q`、`uv run ruff check .` 全綠；(manual) 使用者檢視 README 內容與 GitHub 上的呈現
  - 結果:RED 時 2 則失敗(`test_readme_zh_has_d1_sections_in_order` 缺段落標題、`test_readme_zh_links_architecture_doc` 無架構連結);舊 README 沒有相對連結且已提到 `.env.example`/`docker-compose.yml`,其餘測試與連結檢查器負向案例當時即通過。改寫後 GREEN:`test_readme.py` 17 passed、全套 1123 passed、ruff 全綠。code-review 後修正:Quick Start 與架構連結檢查改為限定在對應段落、標題改為完全比對、連結檢查略過 inline code 並補 `~~~`/`<target>`/title 測試、亮點中「列鎖」措辭依 docs/report 修正。語言切換連結留給 2.1。

## 2. 英文版 README.en.md 與雙向連結

- [ ] 2.1 [RED→GREEN]（design.md D2、D4）擴充 `config/tests/test_readme.py`：`README.en.md` 存在；包含 D1 九個段落的英文標題；`README.md` 頂端連到 `README.en.md`，`README.en.md` 頂端連回 `README.md`；英文版相對連結同樣全部有效；兩份檔案段落數量一致。確認為 FAIL 後，撰寫 `README.en.md`（對應中文主檔，並註明以中文版為準），在 `README.md` 頂端加入語言切換連結 — (auto) `uv run pytest config/tests/test_readme.py -q`、`uv run pytest -q`、`uv run ruff check .` 全綠；(manual) 使用者檢視英文版內容與兩份互相連結
