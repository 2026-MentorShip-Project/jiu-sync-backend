## Purpose

讓前端能對任何非 2xx 回應做一致的程式化判斷（顯示對應文案、決定重試或導頁），不必為每支端點各自處理不同的錯誤形狀。

## ADDED Requirements

### Requirement: 統一錯誤回應形狀
系統 SHALL 讓所有非 2xx 回應的 body 都是 `{"message": string, "code": string|null}` 這個形狀，不因為錯誤來自哪一支 view、哪一種例外類型而有不同的欄位結構。

#### Scenario: 未預期的例外仍符合統一形狀
- **WHEN** 任何 view 拋出一個沒有被特別處理過的例外（例如 DRF 內建的 `NotAuthenticated`）
- **THEN** 回應 body 仍是 `{"message": ..., "code": ...}` 形狀，不是 DRF 預設的 `{"detail": ...}`

### Requirement: 沒有機器可讀代碼時 code 明確為 null
系統 SHALL 在無法歸類出具體錯誤代碼時，將 `code` 明確設為 `null`，而不是省略這個欄位或塞一個籠統的預設字串。

#### Scenario: 通用例外沒有特定 code
- **WHEN** 發生一個沒有指定 `code` 的一般性例外
- **THEN** 回應的 `code` 欄位為 `null`，`message` 欄位帶有可讀的錯誤說明

### Requirement: View 可以指定機器可讀的錯誤代碼
系統 SHALL 讓 view 層在拋出特定業務錯誤時，能附帶一個機器可讀的 `code`（例如 `LINK_EXPIRED`、`EVENT_CANCELLED`），讓前端能依這個值分支處理，不需要解析 `message` 文字。

#### Scenario: View 指定了 code
- **WHEN** view 拋出一個帶有明確 `code` 值的錯誤
- **THEN** 回應的 `code` 欄位就是該值，前端可以依此判斷錯誤種類

### Requirement: 連結失效與資源不存在的狀態碼分離
系統 SHALL 讓「資源不存在」（404）與「資源存在過、但連結已失效」（410）回傳不同的狀態碼，讓前端能分別顯示對應畫面，而不是把兩種情況都當成 404。

#### Scenario: 已失效連結回傳 410 而非 404
- **WHEN** 請求指向一個曾經存在、但已經失效的資源（例如已過期的活動連結）
- **THEN** 系統回傳 410 狀態碼，body 仍符合統一的 `{message, code}` 形狀

### Requirement: 未匹配任何路由的請求也符合統一格式
系統 SHALL 讓「沒有任何 URL 路由匹配」的請求（走不到任何 view）跟「未預期的伺服器錯誤」（走不到任何 view 的例外處理）回應也符合 `{message, code}` 形狀，不是框架預設的 HTML 錯誤頁。

#### Scenario: 不存在的路徑回傳一致格式
- **WHEN** 請求的路徑沒有匹配任何已註冊的 URL
- **THEN** 系統回傳 404，body 是 `{"message": ..., "code": null}` 形狀，不是 HTML

#### Scenario: 真正未被攔截的伺服器錯誤仍回一致格式，但不影響伺服器端可觀測性
- **WHEN** 發生一個連 DRF 例外處理都沒攔到的伺服器錯誤
- **THEN** 回應給使用者的 body 仍是 `{"message": ..., "code": null}` 形狀（狀態碼 500），但伺服器端的錯誤紀錄／日誌不受影響、照常完整記錄，不因為統一了對外格式而讓問題在伺服器端變得不可見
