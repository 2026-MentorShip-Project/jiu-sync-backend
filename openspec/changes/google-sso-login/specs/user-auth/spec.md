## Purpose

讓主揪（host）擁有單一、低摩擦的身份識別，讓平台能把活動、聚會與管理動作歸屬到建立者本人，同時不必自建一整套帳密系統的維運負擔。

## ADDED Requirements

### Requirement: Google SSO 是主揪唯一登入方式
系統 SHALL 只透過 Google Sign-In 驗證主揪身份。系統 SHALL NOT 提供帳密註冊、簡訊 OTP、或安全提示問題等登入方式給主揪使用。

#### Scenario: 不存在其他登入入口
- **WHEN** 主揪在未登入狀態訪問主站
- **THEN** 唯一可用的登入動作是「使用 Google 登入」

### Requirement: Google 身份驗證
系統 SHALL 接受客戶端傳來的 Google 簽發身份權杖，並在核發存取權限前驗證它。當權杖的 audience 與本應用不符、issuer 不是 Google、或權杖已過期／格式不正確時，系統 SHALL 拒絕該權杖。

#### Scenario: 有效的 Google 身份被接受
- **WHEN** 客戶端提交一個由本應用核發對象、尚未過期的 Google 身份權杖
- **THEN** 系統驗證成功並繼續建立 session

#### Scenario: 偽造或錯誤 audience 的權杖被拒絕
- **WHEN** 客戶端提交的權杖 audience 與本應用不符，或已過期、格式錯誤、非 Google 簽發
- **THEN** 系統以身份驗證錯誤拒絕該請求，且不建立任何 session

### Requirement: 主揪 Session 核發
Google 身份權杖驗證通過後，系統 SHALL 以主揪穩定的 Google 帳號識別碼辨識該主揪（首次登入時建立主揪紀錄），並 SHALL 核發一組屬於該主揪的 session 憑證（短效存取憑證＋長效刷新憑證）。

#### Scenario: 首次登入建立主揪紀錄
- **WHEN** 使用從未登入過的 Google 帳號登入
- **THEN** 系統建立一筆與該 Google 帳號關聯的新主揪紀錄，並核發一組 session 憑證

#### Scenario: 回訪主揪沿用既有紀錄
- **WHEN** 使用先前已登入過的 Google 帳號登入
- **THEN** 系統將新的 session 連結到既有的主揪紀錄，而非建立重複紀錄

#### Scenario: 回訪主揪的個人資料隨 Google 端更新同步
- **WHEN** 一個已存在的主揪用 Google 帳號登入，且該次 Google claims 的 email／顯示名稱／頭像與本地既有紀錄不同
- **THEN** 系統將該主揪紀錄的對應欄位更新為本次 claims 的值

### Requirement: Session 刷新
系統 SHALL 讓主揪用有效、未被撤銷的刷新憑證換發新的存取憑證，且不需重新走 Google 登入；系統 SHALL 拒絕已過期或已撤銷的刷新憑證。

#### Scenario: 有效的刷新憑證換得新的存取憑證
- **WHEN** 主揪提交一個尚未過期、也未被撤銷的刷新憑證
- **THEN** 系統核發一個新的存取憑證

#### Scenario: 已撤銷或過期的刷新憑證被拒絕
- **WHEN** 主揪提交一個已過期、或已被撤銷（例如因登出）的刷新憑證
- **THEN** 系統拒絕該請求，且不核發新的存取憑證

### Requirement: 登出撤銷 Session
系統 SHALL 讓主揪透過撤銷其刷新憑證來結束 session；撤銷後，該刷新憑證 SHALL NOT 再被用來取得新的存取憑證。

#### Scenario: 登出使後續刷新失效
- **WHEN** 已登入的主揪執行登出
- **THEN** 其刷新憑證被撤銷，之後任何用它換發新存取憑證的嘗試都會被拒絕

### Requirement: 已登入主揪的身份查詢
系統 SHALL 讓持有有效存取憑證的主揪查詢自己的身份資料（email、暱稱、頭像）。當請求未附帶有效存取憑證時，系統 SHALL 拒絕該請求。

#### Scenario: 已登入主揪讀取自己的資料
- **WHEN** 持有有效存取憑證的主揪查詢自己的身份
- **THEN** 系統回傳該主揪的 email、暱稱與頭像

#### Scenario: 未登入請求被拒絕
- **WHEN** 查詢主揪身份的請求未帶有效存取憑證
- **THEN** 系統以身份驗證錯誤拒絕該請求

### Requirement: 主揪身份驗證不影響參與者存取
主揪身份驗證 SHALL 只作用於主揪管理相關資源，SHALL NOT 被要求用於、或以任何方式限制參與者頁面的存取。

#### Scenario: 參與者存取不受主揪驗證影響
- **WHEN** 參與者在不帶任何主揪憑證的情況下開啟活動連結
- **THEN** 系統提供該活動的參與者頁面內容，不要求主揪憑證
