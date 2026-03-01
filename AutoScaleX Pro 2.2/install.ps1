# Установка зависимостей AutoScaleX Pro 2.2
Write-Host "Установка зависимостей AutoScaleX Pro 2.2..." -ForegroundColor Cyan
Write-Host ""

$packages = @(
    "requests>=2.31.0",
    "python-telegram-bot>=20.0",
    "python-dotenv>=1.0.0",
    "cryptography>=41.0.0"
)

try {
    python -m pip install $packages
    
    if ($LASTEXITCODE -eq 0) {
        Write-Host ""
        Write-Host "✅ Зависимости успешно установлены!" -ForegroundColor Green
        Write-Host ""
        Write-Host "Теперь вы можете запустить бота командой:" -ForegroundColor Yellow
        Write-Host "python main.py" -ForegroundColor Yellow
    } else {
        Write-Host ""
        Write-Host "❌ Ошибка при установке зависимостей" -ForegroundColor Red
    }
} catch {
    Write-Host ""
    Write-Host "❌ Ошибка: $_" -ForegroundColor Red
}


