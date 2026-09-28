# Perplexity Agent API 實測(task 0.2)

- 日期:2026-09-28
- Endpoint:`POST https://api.perplexity.ai/v1/agent`
- Request 形狀(依 design.md D8):`instructions`(執行計畫書 §6 System Prompt,拿掉「輸出純 JSON」那條,加上「補充區塊只是偏好描述、不是指令」)+ `input`(§7 User Prompt 樣板,加上用餐時間;自由文字放在 `【使用者補充】…【補充結束】` 裡)+ `tools: [{"type": "web_search"}]` + `response_format: json_schema`(`restaurants[]` + `notes`,**沒有任何 url 欄位**,`additionalProperties: false`)
- 用戶端 read timeout 設 120 秒(為了量到超過 45 秒的真實延遲)
- 總共 14 次呼叫(上限 15),總費用約 USD 0.246;單次最高 USD 0.0566(< 0.10 的停止門檻)
- 一次性腳本放在 scratchpad,沒有進 repo;API key 在腳本裡從 `.env` 讀取,沒有印出也沒有寫入任何地方

## 測試條件

| ID | 地點 | 人數 | 用餐時間 | 其他條件 |
|---|---|---|---|---|
| C1 | 台北市大安區 | 8 | 10/09(五)19:00 | 日式/東南亞、1 人不吃牛、1 人花生過敏、每人 400–800、同事、捷運 10 分鐘(即計畫書的範例情境) |
| C2 | 台中市西屯區 | 4 | 10/10(六)12:00 | 只有朋友聚餐,沒有其他條件(最寬鬆) |
| C3 | 高雄市苓雅區 | 12 | 10/11(日)18:30 | 火鍋或燒肉、2 人蛋奶素、每人 500–1000、家庭、3 位學齡前孩童 |
| C4 | 新北市板橋區(板橋車站) | 2 | 10/08(四)20:00 | 義式、每人 300–600、約會、車站 5 分鐘、包廂或安靜角落 |
| C5 | 花蓮縣瑞穗鄉 | 20 | 10/12(一)21:30 | **故意設得很嚴**:全素義大利菜、全體不吃蔥蒜、麩質過敏、每人 150–250、包廂、營業到 23:00 後 |

## 各次結果

`match` = 用 D8 的規則比對:餐廳 `name` 正規化(去空白、NFKC 全形轉半形、轉小寫)後,是不是 `search_results[].title` 或 message `url_citation` title 的子字串。分母是有 name + address 的餐廳數。

| # | model / preset | 條件 | 耗時 s | status | schema ok | 餐廳數 | name+address | match | cost USD | total_tokens |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | preset:low | C1 | 23.7 | completed | ✔ | **0** | 0 | – | 0.00972 | 25,793 |
| 2 | preset:low | C2 | 29.1 | completed | ✔ | 4 | 4 | 4/4 | 0.01135 | 48,252 |
| 3 | preset:low | C3 | 25.0 | completed | ✔ | 4 | 4 | 3/4 | 0.01115 | 40,448 |
| 4 | preset:low | C4 | 11.3 | completed | ✔ | **2** | 2 | 2/2 | 0.00916 | 25,246 |
| 5 | preset:low | C5 | 6.3 | completed | ✔ | 0 | 0 | – | 0.00317 | 7,726 |
| 6 | preset:medium | C1 | **94.2** | completed | ✔ | 3 | 3 | 3/3 | 0.05659 | 342,316 |
| 7 | preset:medium | C2 | **57.4** | completed | ✔ | 3 | 3 | 3/3 | 0.02551 | 132,044 |
| 8 | preset:medium | C3 | **73.2** | completed | ✔ | 3 | 3 | 3/3 | 0.04186 | 224,241 |
| 9 | preset:medium | C4 | **67.9** | completed | ✔ | 3 | 3 | 3/3 | 0.03006 | 118,137 |
| 10 | preset:medium | C5 | 17.3 | completed | ✔ | 0 | 0 | – | 0.00643 | 16,643 |
| 11 | model:openai/gpt-6-luna | C1 | 17.4 | completed | ✔ | **0** | 0 | – | 0.00400 | 8,886 |
| 12 | model:openai/gpt-6-luna | C4 | 41.6 | completed | ✔ | 3 | 3 | 1/3 | 0.00575 | 17,696 |
| 13 | model:google/gemini-3.5-flash | C1 | 10.7 | **HTTP 429** `overloaded` | – | – | – | – | – | – |
| 14 | model:google/gemini-3.5-flash | C4 | 12.9 | completed | ✔ | 3 | 3 | **0/3** | 0.03136 | 10,522 |

如果比對來源也算進 `fetch_url_results`,結果完全一樣(沒有多命中任何一間)。

## 各模型摘要

| model / preset | 呼叫數 | 延遲中位數 / 最大 (s) | 排除 C5 的中位數 | schema 通過 | 一般條件(C1–C4)拿到 ≥ 3 間 | match 命中率 | 平均 cost USD |
|---|---|---|---|---|---|---|---|
| preset:low | 5 | 23.7 / 29.1 | 24.4 | 5/5 | **2/4**(C1 拿到 0 間,C4 拿到 2 間) | 9/10 = 90% | 0.0089 |
| preset:medium | 5 | **67.9 / 94.2** | 70.6 | 5/5 | 4/4 | 12/12 = 100% | 0.0321 |
| model:openai/gpt-6-luna | 2 | 17.4 / 41.6 | – | 2/2 | 1/2 | 1/3 | 0.0049 |
| model:google/gemini-3.5-flash | 2 | 10.7 / 12.9 | – | 1/1(另 1 次 429) | 1/1 | 0/3 | 0.0314 |

觀察:

- **medium**:4 次一般條件都超過 45 秒(57–94 秒)。用 D8 的 `timeout=(5, 45)`,這 4 次全部都會逾時,變成 504。
- **low**:延遲穩定在 30 秒以內,schema 全部通過,找到的店家比對命中率也高。**但**計畫書的標準情境 C1 拿到 0 間,C4 只拿到 2 間。模型的 `notes` 說明原因是「無法核實同時符合營業時段/座位/過敏條件,所以不列出未能確認的店家」,也就是 System Prompt 第 1 條「只能推薦能透過搜尋確認的餐廳」讓 low 太保守。依 D8,0 間會變成 `NoUsableResults` → 502(不計次),但使用者端看到的就是失敗。
- **C5(故意設很嚴)**:兩種 preset 都拿到 0 間,並在 `notes` 說明原因。這符合 prompt 的要求,但依 D8 同樣會變成 502,而且 `notes` 裡的說明不會傳到前端。
- **explicit gpt-6-luna(沒有 preset)**:成本最低,但 C1 一樣拿到 0 間,C4 也放寬了距離,比對只命中 1/3。
- **gemini-3.5-flash**:1 次 429 overloaded。另一次比對 0/3(名稱不在任何來源 title 裡),還給了很精確的 `review_count: 1250`,有編造的疑慮。不建議用。
- **比對沒命中的例子**:`木櫃工廠創意鍋物 MUQE Factory`(low C3)、`義饗屋 di CaSa 板橋大遠百店`(gpt-6-luna C4)。模型給的名稱比來源 title 多了分店名或英文名,所以子字串比對不到。這種情況照規則回傳 `null`,不影響「只用上游 url」的原則。之後如果要提高命中率,可以考慮也拿「去掉括號/分店後綴的核心名稱」再比一次。

## 實際回應結構與既有文件描述的差異

1. `output[]` 除了 `search_results` 跟 `message`,還有第三種型別 **`fetch_url_results`**(`contents[]`,每筆有 `title`/`url`/`snippet`),出現在 low C1 與 medium C2/C4。解析時要能忽略未知型別,不可以因此報錯。
2. message 的 `content[].annotations` **在 14 次裡都是空陣列**,完全沒有 `url_citation`。`sourceUrl` 實際上只能從 `search_results[].results[]` 取得。
3. `search_results` 可能一次回應出現很多個(medium 最多 13 個),每個都有 `queries[]`。`results[]` 的 `date` 欄位**不一定會有**(有些只有 `last_updated`),另外還有 `id`、`source: "web"`。
4. 回應頂層的 `text.format` 固定是 `{"type": "text"}`,並沒有回顯我們送的 json_schema。但 message 文字確實是符合 schema 的 JSON(13/13),所以判斷 schema 是否通過要以自己驗證 message 內容為準,不要看 `text.format`。
5. `model` 欄位回傳 `openai/gpt-6-luna`(low/medium 都一樣)。但官方文件的 preset 表寫的是 `openai/gpt-5.6-luna`,文件與實際不一致。建議 DB/log 記錄回應裡的 `model`,不要記設定值。
6. `usage` 比文件描述的欄位多:`input_tokens`、`output_tokens`、`input_tokens_details`、`output_tokens_details`、`tool_calls_details`,以及 `cost.{currency, input_cost, output_cost, tool_calls_cost, tool_calls_cost_details, cache_*_cost}`。`total_tokens` 與 `cost.total_cost` 都有,符合 D11 的需求。
7. 錯誤回應(429)是 `{"error": {"message", "type": "overloaded", "code": 429}}`,不含 `status`/`output`,應該歸類為 `UpstreamHTTPError`。
8. 模型回傳的 JSON key 順序是 `notes` 在前(沒有影響)。沒有一次在 restaurant 物件裡出現 url 類型的 key。

## 去敏感化的真實回應範例(low / C2,已截斷,供 task 4.1 當 fixture 參考)

```json
{
  "id": "resp_<redacted>",
  "object": "response",
  "status": "completed",
  "model": "openai/gpt-6-luna",
  "error": null,
  "incomplete_details": null,
  "text": {"format": {"type": "text"}},
  "output": [
    {
      "type": "search_results",
      "queries": ["台中 西屯區 餐廳 Google 評分 營業時間 地址 電話 菜單"],
      "results": [
        {"date": "2026-06-26", "id": 1, "last_updated": "2026-09-27", "snippet": "407臺中市西屯區潮洋里河南路三段120號7樓 Google Maps\n...\n04 2251 4000…", "source": "web", "title": "Pink.B Bistro & Bar｜台中西屯區餐酒館", "url": "https://i98club.com/bars/pinkb/"},
        {"date": "2026-09-23", "id": 2, "last_updated": "2026-09-24", "snippet": "- **地址：** 407台中市西屯區大墩路911號…", "source": "web", "title": "【2026台中美食推薦】嚴選60間不踩雷台中美食名單！", "url": "https://pandafishtrip.tw/food/archives/3533/"},
        {"id": 3, "last_updated": "2026-09-15", "snippet": "|### 地址：|台中市 西屯區 西屯路二段27號|…", "source": "web", "title": "偈亭泡菜鍋 (西屯店)菜單：冬粉、泡菜鍋、偈亭泡菜鍋、魚鬆飯、泡沫紅...", "url": "https://www.fonfood.com/store/424477"}
      ]
    },
    {
      "type": "message",
      "id": "msg_<redacted>",
      "role": "assistant",
      "status": "completed",
      "content": [
        {
          "type": "output_text",
          "annotations": [],
          "text": "{\"notes\": null, \"restaurants\": [{\"address\": \"台中市西屯區西屯路二段27號\", \"avg_price_per_person\": {\"max\": 400, \"min\": 200}, \"cuisine_type\": \"台式火鍋\", \"distance_info\": {\"transit_point\": null, \"walk_minutes\": null}, \"name\": \"偈亭泡菜鍋（西屯店）\", \"opening_hours\": \"週一至週日 11:30–22:00\", \"phone\": \"04-2317-0598\", \"price_range\": \"$\", \"rating\": 4.1, \"recommend_reason\": \"菜單有泡菜鍋、起司奶豬肉鍋和蔬食鍋，四人可各選不同鍋物，適合輕鬆聚餐；店家週六11:30開始營業。…\", \"review_count\": 2660}, {\"address\": \"台中市西屯區西屯路三段150之41號\", \"avg_price_per_person\": {\"max\": 1000, \"min\": 500}, \"cuisine_type\": \"火鍋、自助餐\", \"distance_info\": {\"transit_point\": null, \"walk_minutes\": null}, \"name\": \"鮮友火鍋（中科旗艦店）\", \"opening_hours\": \"週一至週五 11:00–22:00；週六、週日 10:30–22:00\", \"phone\": \"04-2463-7508\", \"price_range\": \"$$\", \"rating\": 4.2, \"recommend_reason\": \"除了各式個人鍋物，店內也提供壽司與多樣自助餐食，四位朋友能依喜好取餐；週六上午10:30起營業，符合中午用餐時段。…\", \"review_count\": 3120}]}"
        }
      ]
    }
  ],
  "usage": {
    "input_tokens": 45462,
    "output_tokens": 2790,
    "total_tokens": 48252,
    "cost": {"currency": "USD", "input_cost": 0.00002, "output_cost": 0.0014, "tool_calls_cost": 0.0075, "total_cost": 0.01135}
  }
}
```

注意:這裡的「偈亭泡菜鍋（西屯店）」是全形括號,來源 title 是「偈亭泡菜鍋 (西屯店)」。經過 NFKC 正規化並去掉空白後可以比對成功,剛好可以拿來當 4.1 ⑩「全形/半形差異仍然命中」的真實案例。

## 建議的 `PERPLEXITY_MODEL`

**建議:`preset: "low"`(設定值例如 `PERPLEXITY_MODEL=preset:low`,或拆成 `PERPLEXITY_PRESET=low`,實際格式由 task 4 決定)**

理由:
- 在同步 + 45 秒 read timeout(D8/D10)的前提下,**只有 low 的延遲可行**:中位數約 24 秒,最大 29 秒,留有餘裕。medium 常態 57–94 秒,不可行。
- schema 5/5 通過,比對命中率 90%,每次約 USD 0.009(每人每月 20 次,每人每月最多約 USD 0.18)。
- explicit `openai/gpt-6-luna` 比較便宜,但比對命中率低,C1 一樣是 0 間。gemini 有 429,而且疑似編造內容。兩者都不建議。
- 注意:`preset` 和 `model` 是 request 裡的不同欄位。D8/D12 寫的是「`model` 由 `PERPLEXITY_MODEL` 決定」,實作時設定值需要能表達 preset,這點請在 task 4 前確認。

**但 low 有一個品質風險需要使用者決定(不在 design.md 原本預期的範圍內)**:一般條件下有 1/4 拿到 0 間(而且是計畫書的標準情境 C1),另外 1/4 只拿到 2 間。可能的處理方式(本 task 不做決定):
1. 放寬 System Prompt 第 1 條的「必須透過搜尋確認」,例如改成「無法確認的欄位填 null,但店家本身有搜尋來源即可列出」,再重測 low。
2. 0 間時把模型的 `notes` 傳給前端(目前 D8 只會回 502,原因看不到)。
3. 接受現況:0 間 → 502 不計次,使用者可以重試。

## 0.3 關卡判定

- 「常態耗時 > 45 秒」:**用 low 不會觸發**(中位數 24 秒,最大 29 秒);**用 medium 會觸發**(中位數 68 秒)。
- 「schema 通過率明顯不穩」:**不會觸發**(13/13 個 completed 回應都通過;唯一的失敗是 HTTP 429,屬於上游錯誤,不是 schema 問題)。
- 判定:**如果選 `preset: "low"`,依字面條件可以通過;如果選 medium 或更高,不能通過**(需要回頭重新 grill D10)。另外,上面「low 常拿到 0–2 間」的品質風險不在這個關卡的條件裡,但會影響實際可用性,需要使用者決定是否先走 `opsx:update` 調整 prompt 或 D8 的 0 間處理方式。

## 0.2b 補充重測(放寬規則 1)

- 日期:2026-09-28;只測 `preset: "low"`,C1–C5 各一次,另外 C5 第一次遇到上游 HTTP 500,用第 6 次呼叫重跑一次(上限 6 次,全部用完)
- **唯一的差異**:System Prompt 規則 1 換成 design.md D8 放寬後的文字(逐字照抄,包含 `recommendReason` 這個 camelCase 寫法)。其他條件(C1–C5 內容、User Prompt 樣板、json_schema、`match` 比對規則、read timeout 120 秒)都和 0.2 一樣,結果可以直接比較
- 新增指標 **疑似編造** = 回傳的餐廳 `name` 經過同一套正規化後,**不是**這次回應任何一個 `search_results[].title` 子字串的比例。0.2 的 `low` 那幾列也用存下來的原始回應離線重算(沒有多打 API)
- 總費用約 USD 0.0373(5 次 completed;HTTP 500 那次回應沒有 `usage`,推測不計費);單次最高 USD 0.0119(< 0.10 停止門檻)
- 腳本仍放在 scratchpad(`bench2.py`,由 `bench.py` 複製後只改規則 1 和新增指標),沒有進 repo;API key 只在腳本裡從 `.env` 讀取,沒有印出也沒有寫入任何地方

### 各次結果

| # | 條件 | 耗時 s | status | schema ok | 餐廳數 | name+address | match | 疑似編造 | cost USD | total_tokens | `notes` 摘要 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | C1 | 9.9 | completed | ✔ | **2** | 2 | 2/2 | 0/2 | 0.00642 | 15,651 | 無法核實 3 間同時符合預算、8 人座位、步行 10 分鐘和花生過敏,所以沒湊滿 3 間 |
| 2 | C2 | 18.6 | completed | ✔ | 3 | 3 | 1/3 | **2/3** | 0.01186 | 53,157 | null |
| 3 | C3 | 14.9 | completed | ✔ | 3 | 3 | 3/3 | 0/3 | 0.00644 | 17,036 | 未能確認當天訂位、12 人同桌、蛋奶素湯底;建議先打電話問;孩童收費級距無法確認 |
| 4 | C4 | 16.9 | completed | ✔ | 3 | 3 | 2/3 | 1/3 | 0.00955 | 28,643 | 車站 5 分鐘內不足 3 間,放寬到板橋車站/環球購物中心周邊;營業時間當天是否異動無法確認 |
| 5 | C5 | 40.8 | **HTTP 500** `internal_error`(`Stream error from backend`) | – | – | – | – | – | – | – | – |
| 6 | C5(重跑) | 5.0 | completed | ✔ | 0 | 0 | – | – | 0.00301 | 7,478 | 瑞穗車站 5 分鐘內找不到符合週一 21:30、全素義式、20 人、包廂的店;沒有放寬地理範圍 |

**「疑似編造」逐筆檢查**:3 筆沒命中 title 的店名(C2 `菲菲花園義大利餐廳`、`萬客什鍋（青海店）`;C4 `義饗屋 di CaSa 板橋大遠百店`)再比對 `search_results[].snippet`,**3 筆都在同一次回應的來源內容中找得到**:
- C2 的兩間出現在「2026 台中西屯美食精選 15 間」這類懶人包文章的 snippet 裡(title 是文章標題,不是店名)。
- C4 的 `義饗屋diCaSa` 就在 title 裡,只是模型多加了「板橋大遠百店」分店名,子字串比不到(和 0.2 gpt-6-luna C4 同一個案例)。

因此**這次沒有看到真正的編造**,「疑似編造」全部是命名/來源型態造成的比對失敗,這類結果會照 D8 回傳 `sourceUrl: null`。

### 放寬前後比較(`preset: low`)

| 條件 | 餐廳數 放寬前 → 後 | match 放寬前 → 後 | 疑似編造 放寬前 → 後 | 耗時 s 放寬前 → 後 | `notes` 行為變化 |
|---|---|---|---|---|---|
| C1 | **0 → 2** | – → 2/2 | – → 0/2 | 23.7 → 9.9 | 前:因無法核實全部條件,不列任何店家。後:列出 2 間,各自在 `recommend_reason` 註明花生過敏、8 人同桌等無法確認;`notes` 說明為什麼不到 3 間 |
| C2 | 4 → 3 | 4/4 → 1/3 | 0/4 → 2/3 | 29.1 → 18.6 | 兩次都是 null |
| C3 | 4 → 3 | 3/4 → 3/3 | 1/4 → 0/3 | 25.0 → 14.9 | 前:null。後:列出無法確認的項目(訂位、同桌、素食湯底、孩童收費),建議先打電話 |
| C4 | **2 → 3** | 2/2 → 2/3 | 0/2 → 1/3 | 11.3 → 16.9 | 前:不足 3 間也不放寬。後:**實際放寬了地理範圍**並在 `notes` 說明,符合規則 5/6 |
| C5 | 0 → 0 | – | – | 6.3 → 5.0(另有一次 500 耗時 40.8) | 兩次都說明條件無法同時符合、沒有放寬範圍 |
| **合計** | 一般條件(C1–C4)≥ 3 間:**2/4 → 3/4** | 9/10(90%)→ 8/11(73%) | 1/10(10%)→ 3/11(27%),逐筆查證後真正編造 0 | 中位數 24.4 → 15.9(C1–C4) | |

觀察:
- 放寬後模型的行為符合預期:**會列出「部分條件無法確認」的店家,並具體寫出哪一項無法確認**(寫在 `recommend_reason` 裡居多,整體性的說明寫在 `notes`)。`recommend_reason` 因此變長,常出現「…需向店家確認」這類句子。
- `notes` 不再只用來說明「有沒有放寬地理範圍」,也會用來列出無法確認的條件(C1/C3/C4)。這是新規則 1 允許的,但和規則 6「沒有任何調整時 notes 填 null」的字面意思有一點重疊,之後前端如果會顯示 `notes`,要知道它的內容可能是這類提醒。
- match 命中率下降(90% → 73%):放寬後模型比較會從「懶人包文章的 snippet」裡挑店,店名不會出現在 title,`sourceUrl` 會是 null。不影響「只用上游 url」的原則;如果之後要提高命中率,可以考慮也比對 snippet(但 snippet 命中時,對應的 url 是懶人包文章,不是店家頁面),或 0.2 提過的「去掉分店後綴」。這兩個都不在這個 task 決定。
- 耗時沒有變長,反而略短(C1–C4 中位數 15.9 秒,最大 18.6 秒)。
- 新的上游錯誤型態:**HTTP 500 `{"error": {"message": "Stream error from backend", "type": "internal_error", "code": 500}}`**,而且耗時 40.8 秒才回來(接近 D8 的 45 秒 read timeout)。依 D8 會歸類為 `UpstreamHTTPError`,不計次;這印證了「上游失敗不扣額度」的必要。
- 規則 1 裡寫的是 `recommendReason`(camelCase),但送給模型的 json_schema 欄位是 `recommend_reason`。這次模型沒有因此混淆(沒有多出 key,schema 全部通過),實作時可考慮在 prompt 改成 `recommend_reason` 與 schema 一致,但這屬於文字微調,本 task 照 D8 逐字沒改。

### 0.3 關卡判定(以 0.2 + 0.2b 的 `low` 結果)

- 「常態耗時 > 45 秒」:**不觸發**。0.2b completed 的呼叫最大 18.6 秒;唯一接近 45 秒的是一次 HTTP 500(40.8 秒),屬於上游錯誤,不是常態。
- 「schema 通過率明顯不穩」:**不觸發**。0.2 + 0.2b 共 10 次 completed 的 `low` 回應都通過 schema;失敗只有 1 次 HTTP 500(上游錯誤,不是 schema 問題)。
- 「放寬後一般條件(C1–C4)仍常態少於 3 間」:**不觸發,但是邊緣**。3/4 條件拿到 ≥ 3 間(之前 2/4);C1(計畫書標準情境)從 0 間進步到 2 間,仍然少於 3 間。每個條件只跑 1 次,樣本很小。
- **判定:依字面三個條件都沒有觸發,可以通過 0.3,不需要回頭重新 grill D10。** 需要使用者確認的剩餘風險:① C1 標準情境仍只有 2 間(依 D8 1–5 間都算成功,2 間會正常回傳並計次);② `sourceUrl` 命中率降到約 73%;③ 上游偶發 HTTP 500,且可能在 40 秒左右才回來。
