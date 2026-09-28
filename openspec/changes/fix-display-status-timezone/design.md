## Context

- `apps/events/lifecycle.compute_display_status` 是純函式,由活動詳情、活動列表 serializer 與其他以活動狀態為前置檢查的 view 共用;呼叫端一律傳入 `timezone.now()`(aware、UTC)。
- 目前以 `now.date()` 取日期,得到的是 UTC 日期;整個 repo 只有這一處以 UTC 日期與「今天」比較。
- `USE_TZ = True`、`TIME_ZONE = "Asia/Taipei"`。

## Goals / Non-Goals

**Goals:**
- 日期比較以台灣時間為準,所有呼叫端自動修正。

**Non-Goals:**
- 不改 `finalized_at`/`cancelled_at` 的 7 天過期判斷(datetime 相減,與時區無關)。
- 不改呼叫端介面、API 回應形狀或錯誤代碼。

## Decisions

- **D1. 在 `compute_display_status` 內改用 `timezone.localdate(now)`**(grill Q-b):只改一處,呼叫端不動;函式仍為純函式(只讀 `settings.TIME_ZONE`,不碰 DB)。
  - 替代:呼叫端改傳本地日期——需改所有呼叫端,之後新增的呼叫端仍可能再犯,不採用。
- **D2. naive `now` 不另外處理**:`timezone.localdate` 對 naive datetime 會丟 `ValueError`,沿用此行為(`USE_TZ=True` 下 `timezone.now()` 必為 aware),以測試固定此行為,避免被默默當成 UTC。
- **D3. 分支**:從 `develop` 開 `fix/display-status-timezone`(grill Q-a),PR 進 `develop`,之後 merge 回 `feature/ai-pick`。

## Risks / Trade-offs

- [既有測試以 UTC 日期建立「今天/昨天」的資料,修正後在台灣時間 00:00–08:00 反而失敗] → 實作時全套測試需在固定 00:30 +08:00 的時間下驗證一次(以測試內固定 `now` 或暫時固定時鐘),發現依賴 UTC 日期的測試資料,屬測試資料缺陷,改用台灣時間的日期並於 incident log 記錄。
