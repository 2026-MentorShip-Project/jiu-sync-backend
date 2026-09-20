> TDD 排法:每個 seam 都是「先寫會失敗的測試(RED)→ 只寫剛好讓它通過的最小實作(GREEN)」一組,垂直切,不要把所有測試一次寫完才開始實作。Seam 範圍:`compute_display_status` 純函式單獨測;`POST /api/events`、`GET /api/events/{id}`、`GET /api/events?owner=me` 三個走 HTTP 層測。
>
> 這個 change 不引入新工具鏈/新依賴(Django/DRF/Postgres 皆已建立,沿用既有 `apps.accounts` 的 JWT 認證與 `config/exceptions.py` 錯誤處理),依規則不需要獨立的環境健檢 task。
>
> 每項驗證方式前綴 `(auto)`/`(manual)`:這裡全部是後端 model/API,全部可由 `pytest`/`ruff`/`manage.py` 指令自動驗證,沒有需要人工操作確認的步驟。

## 1. 前置:Settings 與 Model(非 seam,不走 TDD cycle)

- [x] 1.1 在 `config/settings/base.py` 新增 `FRONTEND_BASE_URL = env("FRONTEND_BASE_URL", default=None)`;`config/settings/dev.py` 覆寫預設值為 `"http://localhost:5173"`;`config/settings/prod.py` 比照既有 `DJANGO_SECRET_KEY`/`ALLOWED_HOSTS` 的 fail-fast 慣例,未設定時啟動即拋錯 — (auto) `python manage.py check`(dev 設定下無錯);另跑一次刻意不帶 `FRONTEND_BASE_URL` 環境變數的 `DJANGO_SETTINGS_MODULE=config.settings.prod python manage.py check`,確認明確報錯而非靜默通過
- [x] 1.2 實作 `apps/events/models.py`:`Event`(UUID pk、`owner` FK User、`title`、`host_nickname`、`host_email` nullable、`mode`、`response_deadline`、`location` nullable、`description` nullable、`status` choices `active`/`finalized`/`cancelled` default `active`、`finalized_at` nullable、`cancelled_at` nullable、`final_slot_id` FK Slot nullable SET_NULL、`final_note` nullable、`created_at`/`updated_at`)與 `Slot`(UUID pk、`event` FK Event related_name=`slots`、`date`、`time` nullable、`label` nullable)— (auto) `python manage.py check` 無錯
- [x] 1.3 產生並套用 migration(`makemigrations events && migrate`),對本機 Postgres(docker compose)跑過 — (auto) migration 指令 exit code 0、無錯誤輸出;`python manage.py migrate events --check` 確認無待套用的 migration

## 2. Seam: `compute_display_status` 純函式(對應 spec 需求:活動顯示狀態衍生計算)

- [x] 2.1 [RED] 在 `apps/events/tests/test_lifecycle.py` 寫測試,涵蓋六個分支:① `status=active` 且 now < deadline → `voting_open`;② `status=active` 且 now >= deadline → `voting_closed_pending`;③ `status=finalized`、`finalized_at` 距今 < 7 天、尚未到最終場次日期 → `finalized_upcoming`;④ `status=finalized`、`finalized_at` 距今 < 7 天、已過最終場次日期 → `finalized_past`;⑤ `status=finalized`、`finalized_at` 距今 >= 7 天 → `link_expired`;⑥ `status=cancelled`、`cancelled_at` 距今 < 7 天 → `cancelled`;⑦ `status=cancelled`、`cancelled_at` 距今 >= 7 天 → `link_expired`。確認這組測試現在是 FAIL(`compute_display_status` 還不存在)— (auto) `pytest` 顯示這幾條 FAIL
- [x] 2.2 [GREEN] 在 `apps/events/lifecycle.py` 實作 `compute_display_status(status, response_deadline, finalized_at, cancelled_at, final_slot_date, now)`,只寫到讓 2.1 全部轉綠為止,不多加東西——滿足需求「活動顯示狀態衍生計算」 — (auto) `pytest` 該檔全綠

## 3. Seam: `POST /api/events`(對應 spec 需求:主揪建立活動)

- [x] 3.1 [RED] 在 `apps/events/tests/test_views.py` 寫 API 測試(走 DRF test client,`Authorization: Bearer` 帶合法 access token):① 已登入使用者送出合法欄位 → 201,body 含 `id`/`shareUrl`,`shareUrl` 以 `settings.FRONTEND_BASE_URL` 開頭,資料庫多一筆 `Event`(`owner` 為該登入使用者、`status="active"`)與對應的 `Slot`;② 未登入(不帶 token)→ 401,不建立任何資料;③ `responseDeadline` 等於或早於送出當下時間 → 400 驗證錯誤,不建立任何資料;④ `slots` 為 0 筆或超過 20 筆 → 400,不建立任何資料;⑤ `title` 超過 30 字元 → 400;⑥ `hostNickname` 加權長度(CJK 字元計 2、其餘計 1)超過 40 → 400,並用一筆剛好等於 40 的邊界案例確認通過;⑦ 請求 body 的 `slots[].id` 帶入任意字串 → 建立成功後,資料庫裡對應 `Slot` 的 id 是系統產生的 UUID,不等於請求傳入的值;⑧ 請求 body 帶入 `hostEmail` → 建立後該筆 `Event.host_email` 仍為 `None`,不採信這個欄位。確認這組測試現在是 FAIL(view 還沒接上)— (auto) `pytest` 顯示這幾條 FAIL
- [x] 3.2 [GREEN] 實作 `EventCreateSerializer`(巢狀 `SlotCreateSerializer`,`validate_response_deadline`/`validate_host_nickname` 等欄位驗證,忽略 `id`/`hostEmail` 輸入)與 `EventCreateView`(`permission_classes=[IsAuthenticated]`,`owner=request.user`,成功回傳 `{"id": ..., "shareUrl": f"{settings.FRONTEND_BASE_URL}/events/{event.id}"}`,狀態碼 201),掛到 `apps/events/urls.py`,讓 3.1 全部轉綠——滿足需求「主揪建立活動」 — (auto) `pytest` 該檔全綠

## 4. Seam: `GET /api/events/{id}`(對應 spec 需求:查詢單一活動完整資料)

- [x] 4.1 [RED] 在 `apps/events/tests/test_views.py` 補測試:① 未登入請求已存在的活動 → 200,`isOwner=false`,`hostEmail=null`;② 活動擁有者本人請求 → 200,`isOwner=true`,`hostEmail` 為該活動實際填寫的值(若為 None 則回傳 null);③ 其他已登入(非擁有者)使用者請求 → 200,`isOwner=false`,`hostEmail=null`;④ 請求不存在的 id → 404,body 符合 `api-error-format` 的 `{message, code}` 形狀;⑤ 回應含 `displayStatus` 欄位,值域對齊 `compute_display_status` 的計算結果(用一筆 `responseDeadline` 已過期的活動驗證回傳 `voting_closed_pending`);⑥ 回應的 `responses` 欄位固定為空陣列 `[]`。確認這組測試現在是 FAIL — (auto) `pytest` 顯示這幾條 FAIL
- [x] 4.2 [GREEN] 實作 `EventDetailSerializer`(`slots` 巢狀序列化、`responses` 固定回傳 `[]`、`isOwner`/`displayStatus` 為 `SerializerMethodField`、`hostEmail` 依 `isOwner` 遮罩)與 `EventDetailView`(`permission_classes=[AllowAny]`,`get_object_or_404` 查無資料時交給既有 `handler404`),掛到 `urls.py`,讓 4.1 全部轉綠——滿足需求「查詢單一活動完整資料」 — (auto) `pytest` 該檔全綠

## 5. Seam: `GET /api/events?owner=me`(對應 spec 需求:主揪查詢自己擁有的活動清單)

- [x] 5.1 [RED] 在 `apps/events/tests/test_views.py` 補測試:① 已登入使用者查詢,建立 A 使用者兩筆活動(其中一筆 `status="cancelled"`)、B 使用者一筆活動,用 A 的 token 查詢 → 回應只含 A 的兩筆活動(含已取消那筆),不含 B 的;② 每筆回應為精簡格式:不含 `responses`/`hostEmail` 欄位,含 `responseCount`;③ 未登入(不帶 token)查詢 → 401;④ 缺少 `owner=me` 查詢參數 → 400 或視為未提供必要參數的驗證錯誤(依 DRF 慣例擇一並在測試中明確斷言)。確認這組測試現在是 FAIL — (auto) `pytest` 顯示這幾條 FAIL
- [x] 5.2 [GREEN] 實作 `EventSummarySerializer`(`responseCount` 固定回傳 `0`、`displayStatus`/`isOwner` 同 4.2 邏輯、不含 `responses`/`hostEmail`)與 `EventListView`(`permission_classes=[IsAuthenticated]`,`queryset` 過濾 `owner=request.user`,不因 `status` 排除任何資料),掛到 `urls.py`,讓 5.1 全部轉綠——滿足需求「主揪查詢自己擁有的活動清單」 — (auto) `pytest` 該檔全綠

## 6. 收尾

- [x] 6.1 跑 `uv run ruff check .` 跟 `uv run python manage.py check`,確認兩者都乾淨無誤;跑全部測試(`uv run pytest`)確認整組綠燈 — (auto) 三個指令 exit code 皆 0
- [x] 6.2 驗證邊界需求「未涵蓋範圍不得被誤實作」:檢視這次 change 的異動檔案清單,確認沒有新增 `PATCH /api/events/{id}`、finalize/cancel、`/live`、`ParticipantResponse` 相關的任何路由或 model — (auto) `git diff --stat develop...HEAD -- apps/events` 顯示的檔案清單與異動內容不含上述項目
