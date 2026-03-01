# 🚀 Инструкция по запуску AutoScaleX Pro 2.2

## Шаг 1: Подготовка

### 1.1. Установите зависимости (если еще не установлены)

**Вариант A - Скрипт установки:**
```bash
cd "AutoScaleX Pro 2.2"
install.bat
```

**Вариант B - Вручную:**
```bash
cd "AutoScaleX Pro 2.2"
pip install -r requirements.txt
```

**Вариант C - Прямая установка:**
```bash
pip install requests python-telegram-bot python-dotenv cryptography
```

### 1.2. Создайте файл `.env`

Скопируйте `env.example` в `.env`:

**Windows:**
```bash
cd "AutoScaleX Pro 2.2"
copy env.example .env
```

**Linux/Mac:**
```bash
cd "AutoScaleX Pro 2.2"
cp env.example .env
```

### 1.3. Настройте `.env` файл

Откройте файл `.env` и заполните:

```env
# Telegram Bot (ОБЯЗАТЕЛЬНО)
TG_TOKEN=ваш_telegram_бот_токен
TG_ADMIN_ID=ваш_telegram_id

# BingX API (можно настроить позже через бота)
BINGX_API_KEY=your_bingx_api_key
BINGX_SECRET=your_bingx_secret
BINGX_SANDBOX=false

# Trading Settings
SYMBOL=ETH-USDT
MIN_ORDER=20
```

**Как получить Telegram токен:**
1. Найдите бота @BotFather в Telegram
2. Отправьте `/newbot`
3. Следуйте инструкциям
4. Скопируйте полученный токен в `TG_TOKEN`

**Как получить Telegram ID:**
1. Найдите бота @userinfobot в Telegram
2. Отправьте `/start`
3. Скопируйте ваш ID в `TG_ADMIN_ID`

## Шаг 2: Запуск бота

### Вариант 1: Запуск из папки проекта (рекомендуется)

```bash
cd "AutoScaleX Pro 2.2"
python main.py
```

### Вариант 2: Запуск с полным путем

```bash
python "AutoScaleX Pro 2.2\main.py"
```

### Вариант 3: Создайте файл `start.bat` для удобства

Создайте файл `start.bat` в папке "AutoScaleX Pro 2.2":

```batch
@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Запуск AutoScaleX Pro 2.2...
python main.py
pause
```

Затем просто дважды кликните на `start.bat`

## Шаг 3: Проверка работы

После запуска вы должны увидеть:

```
==================================================
AutoScaleX Pro 2.2 Starting...
==================================================
Starting Telegram bot...
Telegram bot started successfully
```

Если видите ошибки:
- Проверьте, что `.env` файл создан и заполнен
- Убедитесь, что все зависимости установлены
- Проверьте логи в папке `logs/`

## Шаг 4: Первое использование

### Для администратора:

1. Откройте Telegram бота
2. Отправьте `/start`
3. Отправьте `/admin`
4. Добавьте рефералов через "➕ Добавить реферала"
5. Введите UID реферала

### Для пользователей:

1. Откройте Telegram бота
2. Отправьте `/start`
3. Введите ваш UID (должен быть добавлен администратором)
4. Отправьте `/set_api` и введите:
   - BingX API Key
   - BingX Secret
5. Используйте меню для управления ботом

## Остановка бота

Нажмите `Ctrl+C` в терминале для остановки бота.

## Автоматический перезапуск

Бот автоматически перезапускается при падении (до 10 попыток с задержкой 60 секунд).

## Запуск на сервере (Linux)

### Использование screen:

```bash
screen -S autoscalex
cd "AutoScaleX Pro 2.2"
python main.py
# Нажмите Ctrl+A, затем D для отсоединения
```

### Использование systemd (создайте файл `/etc/systemd/system/autoscalex.service`):

```ini
[Unit]
Description=AutoScaleX Pro 2.2 Trading Bot
After=network.target

[Service]
Type=simple
User=ваш_пользователь
WorkingDirectory=/path/to/AutoScaleX-Pro/AutoScaleX Pro 2.2
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Затем:
```bash
sudo systemctl enable autoscalex
sudo systemctl start autoscalex
sudo systemctl status autoscalex
```

## Логи

Логи сохраняются в папке `logs/`:
- `bot_YYYYMMDD.log` - дневные логи

## Проблемы?

1. **Ошибка "TG_TOKEN not set"** - проверьте `.env` файл
2. **Ошибка импорта модулей** - установите зависимости
3. **Ошибка подключения к Telegram** - проверьте токен
4. **Ошибка API BingX** - проверьте API ключи

Проверьте логи в папке `logs/` для детальной информации об ошибках.


