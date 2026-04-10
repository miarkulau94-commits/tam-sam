# Установка зависимостей AutoScaleX Pro 2.2 (как install.bat: полный requirements.txt)
$ErrorActionPreference = "Stop"
Write-Host "Установка зависимостей AutoScaleX Pro 2.2..." -ForegroundColor Cyan
Write-Host ""

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

try {
    python -m pip install -r requirements.txt
    if ($LASTEXITCODE -eq 0) {
        Write-Host ""
        Write-Host "✅ Зависимости успешно установлены!" -ForegroundColor Green
        Write-Host ""
        Write-Host "Тесты: python -m pytest tests/ -q" -ForegroundColor Yellow
        Write-Host "Бот:   python main.py" -ForegroundColor Yellow
    } else {
        Write-Host ""
        Write-Host "❌ Ошибка при установке зависимостей (код $LASTEXITCODE)" -ForegroundColor Red
        exit $LASTEXITCODE
    }
} catch {
    Write-Host ""
    Write-Host "❌ Ошибка: $_" -ForegroundColor Red
    exit 1
}
