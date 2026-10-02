## Why

主揪使用建立活動時的暱稱投票，會被既有 `NICKNAME_CONFLICTS_WITH_HOST` 規則拒絕。主揪需要沿用自己的名稱投票，並繼續輸入手機末三碼。

## What Changes

- `POST /api/events/{id}/responses/` 改為選擇性解析 JWT。有效 JWT 對應的 `request.user == event.owner` 時，允許使用 `event.host_nickname` 投票。
- 其他登入使用者與匿名使用者仍不能使用主揪暱稱；暱稱維持 trim 後精確比對。
- 手機末三碼必填與格式驗證、活動狀態與截止時間限制、重複投票限制維持。
- 前端依活動的 `isOwner` 自動帶入並固定 `hostNickname`，初次投票請求須攜帶 JWT。後端仍要求提交 nickname，不自動替換輸入值。

## Impact

- 修改 `ParticipantResponseCreateView` 的 authentication 與 serializer context。
- 修改 `ParticipantResponseCreateSerializer.validate_nickname`，僅主揪豁免暱稱衝突。
- 更新 `events` capability 中初次投票的暱稱規則（取代 `add-participant-responses` 的無條件主揪暱稱禁用規則）。無資料庫 migration。
- 已驗證：事件 view 測試 197 passed，Ruff 與 diff check 通過；測試資料庫清理有其他連線占用警告。
