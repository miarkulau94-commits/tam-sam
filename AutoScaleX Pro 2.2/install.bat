@echo off
chcp 65001 >nul
echo Установка зависимостей AutoScaleX Pro 2.2...
echo.

python -m pip install -r requirements.txt

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ✅ Зависимости успешно установлены!
    echo.
    echo Теперь вы можете запустить бота командой:
    echo python main.py
) else (
    echo.
    echo ❌ Ошибка при установке зависимостей
    pause
)


