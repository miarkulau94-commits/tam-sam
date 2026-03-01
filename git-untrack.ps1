# Убрать из индекса git файлы, которые не должны коммититься (если они уже были добавлены).
# Запуск: из корня репозитория .\git-untrack.ps1
# Если файл не в индексе — git выдаст сообщение, это нормально.

$ErrorActionPreference = 'Continue'
git rm --cached "AutoScaleX Pro 2.2/.env"
git rm --cached "referrals.json"
git rm --cached "pending_referrals.json"
git rm -r --cached "AutoScaleX Pro 2.2/.pytest_cache"
Write-Host "Готово. Проверьте: git status"
