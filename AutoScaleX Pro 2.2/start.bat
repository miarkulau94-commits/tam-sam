@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================
echo   AutoScaleX Pro 2.2 - Запуск бота
echo ========================================
echo.

REM Проверка наличия .env файла
if not exist .env (
    echo ⚠️  Файл .env не найден!
    echo.
    echo Создайте файл .env на основе env.example:
    echo   copy env.example .env
    echo.
    pause
    exit /b 1
)

REM Проверка наличия Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Python не найден!
    echo Установите Python 3.8 или выше
    pause
    exit /b 1
)

echo ✅ Проверки пройдены
echo.
echo Запуск бота...
echo Для остановки нажмите Ctrl+C
echo.
echo ========================================
echo.

python main.py

if errorlevel 1 (
    echo.
    echo ❌ Бот завершился с ошибкой
    echo Проверьте логи в папке logs/
    pause
)


