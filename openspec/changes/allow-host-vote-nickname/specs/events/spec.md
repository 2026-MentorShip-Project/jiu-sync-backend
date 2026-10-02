## MODIFIED Requirements

### Requirement: 初次投票的主揪暱稱驗證

系統 SHALL 在 `POST /api/events/{id}/responses/` 選擇性解析 JWT，並以有效 JWT 對應的 `request.user == event.owner` 判斷主揪身份。當送出的 nickname 去除頭尾空白後與活動的 hostNickname 精確相同時，系統 SHALL 允許主揪投票；對其他使用者 SHALL 回傳 HTTP 400、`NICKNAME_CONFLICTS_WITH_HOST`，且不建立投票。nickname SHALL 繼續為必填欄位，後端不自動替換暱稱。

系統 SHALL 保留手機末三碼必填及三位數字格式驗證、同活動暱稱唯一限制、候選時段驗證，以及活動狀態與投票截止限制。主揪豁免只適用於與 hostNickname 衝突的檢查。

#### Scenario: 主揪以自己的活動暱稱投票

- **WHEN** 有效 JWT 對應活動擁有者，提交的 nickname trim 後等於 hostNickname，且其他欄位與活動條件皆有效
- **THEN** 回傳 HTTP 201，儲存 trim 後的暱稱及手機末三碼雜湊，完整活動回應的 isOwner 為 true

#### Scenario: 其他使用者使用主揪暱稱

- **WHEN** 匿名使用者或其他登入使用者提交 trim 後等於 hostNickname 的 nickname
- **THEN** 回傳 HTTP 400、NICKNAME_CONFLICTS_WITH_HOST，不建立投票

#### Scenario: 主揪未提供有效手機末三碼

- **WHEN** 活動擁有者使用主揪暱稱投票，但缺少 phoneLastThree 或格式不是三位數字
- **THEN** 回傳 HTTP 400，不建立投票

#### Scenario: 主揪重複提交初次投票

- **WHEN** 活動擁有者使用主揪暱稱投票，但同活動已存在該暱稱的投票
- **THEN** 回傳 HTTP 400、NICKNAME_TAKEN，沿用既有更新投票流程

#### Scenario: 無效 JWT 不阻擋匿名投票

- **WHEN** 初次投票請求攜帶無效或過期 JWT
- **THEN** 以匿名使用者處理，仍可提交其他有效暱稱的投票，且不能使用主揪暱稱
