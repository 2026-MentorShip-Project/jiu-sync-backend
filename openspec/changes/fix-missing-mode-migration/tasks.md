> 純 tooling 修正(補一支遺漏的 migration),無行為變更,不需要 TDD red-green 或環境健檢——沒有新程式邏輯要測,只有 Django migration 狀態要對齊。

## 1. 補齊 migration

- [x] 1.1 執行 `python manage.py makemigrations`,產生 `apps/events/migrations/0002_alter_event_mode.py`,讓 `Event.mode` 的 migration 記錄追上目前 model 定義 — (auto) `python manage.py makemigrations --check --dry-run` 顯示 "No changes detected"
- [x] 1.2 套用新 migration 並確認不影響既有資料與測試 — (auto) `python manage.py migrate` 成功套用;`pytest`/`ruff check .`/`python manage.py check` 三者皆乾淨通過
