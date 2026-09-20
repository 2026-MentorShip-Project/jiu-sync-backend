## Why

`develop` 上的 `manage.py makemigrations --check --dry-run` 失敗(CI `test` job 擋下)。原因是先前一次改動拿掉了 `Event.Mode` 的 `TextChoices` 明確 label(`"Date only"`/`"Time slots"`),改成讓 Django 自動從成員名生成(結果是大小寫不同的 `"Date Only"`/`"Time Slots"`)。Django 的 migration 系統會把 `choices=` 的內容算進欄位狀態比對,當時沒有同步產生對應的 migration,導致 model 現狀與已存在的 migration 記錄不一致。

## What Changes

- 補上 `apps/events/migrations/0002_alter_event_mode.py`,讓 `Event.mode` 欄位的 migration 記錄追上目前的 model 定義
- 純粹是 `choices` 顯示文字的內部狀態同步,不改變資料庫欄位型別、不改變任何限制條件、不影響任何已存在的資料

這是純粹的 tooling/維護性修正,沒有任何對外可觀察的行為變化,依 CLAUDE.md「specs 描述行為,行為不變則 spec 不用跟著動」的原則,本次 `.openspec.yaml` 設定 `skip_specs: true`,不產出 spec delta。

## Capabilities

### New Capabilities

(無)

### Modified Capabilities

(無 — 無行為變更,見上方 skip_specs 說明)

## Impact

- 新增 `apps/events/migrations/0002_alter_event_mode.py`
