> 無新工具鏈或依賴,不需環境健檢 task。範圍小,壓縮為 1 個垂直 task(grill Q-a/Q-b 已決策)。

## 1. 顯示狀態以台灣時間判斷聚會日期

- [x] 1.1 [RED→GREEN](Requirement: 活動顯示狀態以台灣時間判斷聚會日期是否已過) 在 `apps/events/tests/test_lifecycle.py` 對 `compute_display_status` 以固定 aware `now` 寫測試:① 台灣 9/29 00:30(UTC 9/28 16:30)定案時段 9/28 → `finalized_past`;② 同時間定案時段 9/29 → `finalized_upcoming`;③ 台灣 9/29 23:59 定案時段 9/29 → `finalized_upcoming`;④ 台灣 9/29 08:00(UTC 00:00)定案時段 9/28 → `finalized_past`;⑤ naive `now` → `ValueError`;另加一則 API 層測試:以固定時間台灣 00:30 查詢活動詳情,昨天定案的活動 `displayStatus` 為 `finalized_past`。確認 ① 與 API 層測試為 FAIL(②③④⑤ 為邊界守護,修正前後皆應通過)後,改 `compute_display_status` 使用 `timezone.localdate(now)`(design.md D1)。另以固定台灣 00:30 的時間跑一次 `apps/events` 測試,確認沒有既有測試依賴 UTC 日期 — (auto) `pytest -q`、`ruff check .`、`manage.py check` 全綠
  - 結果:RED = ①、API 層、⑤ 共 3 則 FAIL(⑤ 與本文預期不符:舊碼 `now.date()` 對 naive now 默默回傳狀態,正是 D2 要防的行為,故修正前為紅燈);②③④ 修正前後皆綠。GREEN = 全套 294 passed、ruff/check 乾淨;固定台灣 00:30 時鐘下 `apps/events` 211 passed,無既有測試依賴 UTC 日期。
