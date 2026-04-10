# Развёртывание AutoScaleX Pro 2.2 на VPS (Ubuntu/Debian)

Пошаговая инструкция по установке бота на сервер (например, Deephost, и т.п.) с автозапуском через systemd.

---

## 1. Подключение к серверу

Подключитесь по SSH (подставьте свой IP и пользователя):

```bash
ssh root@IP_ВАШЕГО_СЕРВЕРА
```

При первом входе может потребоваться пароль или ключ.

---

## 2. Подготовка системы

Обновите пакеты и установите необходимое:

```bash
apt update && apt upgrade -y
apt install -y git python3 python3-pip python3-venv nano
```

Если появится меню **needrestart** (перезапуск служб), можно выбрать **8** (ничего не перезапускать) или **Q** для выхода.

**Если не работает DNS** (не резолвятся имена при `apt update`):

```bash
echo -e "nameserver 8.8.8.8\nnameserver 1.1.1.1" > /etc/resolv.conf
```

---

## 3. Клонирование репозитория

Пример (замените на свой репозиторий и ветку при необходимости):

```bash
cd /root
git clone https://github.com/ВАШ_ЛОГИН/ВАШ_РЕПОЗИТОРИЙ.git tam-sam
cd tam-sam/AutoScaleX\ Pro\ 2.2
```

Или, если папка с ботом уже есть под другим путём:

```bash
cd /root/tam-sam/AutoScaleX\ Pro\ 2.2
```

Дальнейшие команды предполагают, что вы находитесь в каталоге с `main.py` (например `/root/tam-sam/AutoScaleX Pro 2.2`).

---

## 4. Виртуальное окружение и зависимости

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

После этого в начале строки должно быть `(venv)`.

---

## 5. Настройка .env

Создайте конфиг из примера:

```bash
cp env.example .env
```

Отредактируйте `.env`:

```bash
nano .env
```

**Обязательно заполните:**

| Переменная           | Описание |
|----------------------|----------|
| `TG_TOKEN`           | Токен бота от [@BotFather](https://t.me/BotFather) |
| `ENCRYPTION_SECRET`   | Случайная строка **не короче 32 символов** (для шифрования API ключей) |
| `TG_ADMIN_ID`        | Ваш Telegram user ID (число). Узнать можно у [@userinfobot](https://t.me/userinfobot) |

**По желанию:** `BINGX_API_KEY`, `BINGX_SECRET`, `REFERRAL_LINK` и др. — см. комментарии в `env.example`.

### Сохранение в nano

- **Сохранить:** `Ctrl+O`, затем **Enter** (подтвердить имя файла `.env`).
- **Выйти:** `Ctrl+X`.

Если на вашем ПК `Ctrl+O` открывает диалог «Открыть файл», можно не использовать nano: добавить строки через терминал (см. ниже).

### Редактирование .env без nano (через терминал)

Замените значения на свои:

```bash
sed -i 's/^TG_TOKEN=.*/TG_TOKEN=ваш_токен_от_BotFather/' .env
sed -i 's/^ENCRYPTION_SECRET=.*/ENCRYPTION_SECRET=ваша_случайная_строка_минимум_32_символа/' .env
sed -i 's/^TG_ADMIN_ID=.*/TG_ADMIN_ID=ваш_telegram_id/' .env
```

Или добавить строки в конец (если переменных ещё нет):

```bash
echo 'TG_TOKEN=ваш_токен' >> .env
echo 'ENCRYPTION_SECRET=ваш_секрет_32_символа' >> .env
echo 'TG_ADMIN_ID=ваш_id' >> .env
```

Ограничьте доступ к файлу с секретами:

```bash
chmod 600 .env
```

### Синхронизация ордеров и лимиты BingX (опционально)

При расхождении списка ордеров в памяти и на бирже бот опирается на снимок **открытых ордеров** (`open_orders`). Чтобы не вызывать десятки **`get_order`** подряд при лимите API, за один проход синхронизации действует потолок на **`get_order`**; остальные «пропавшие» с биржи обрабатываются как исполнение по цене из памяти (как в основном цикле `check_orders`). Экран **«Баланс»** в Telegram использует более строгий лимит, чтобы не разгонять API при просмотре.

Ошибки **rate limit (429)** и сообщения о превышении лимита запросов **не увеличивают** счётчик открытия **circuit breaker** (временная перегрузка квоты, а не «падение» API).

| Переменная | По умолчанию | Описание |
|------------|---------------|----------|
| `SYNC_GET_ORDER_MAX_PER_CALL` | `10` | Максимум вызовов `get_order` за один `sync_orders_from_exchange` (полный синк в цикле и т.д.). |
| `SYNC_BALANCE_MAX_GET_ORDER` | `3` | То же при открытии баланса из Telegram. |

Задать в `.env` при необходимости, затем перезапустить бота.

---

## 6. Проверка запуска

Запустите бота вручную:

```bash
source venv/bin/activate   # если ещё не активировано
python main.py
```

В логах должно быть: `Telegram bot started successfully`. Проверьте бота в Telegram (команда `/start`). Остановка: **Ctrl+C**.

Сообщения `CRITICAL` / `CancelledError` при остановке **Ctrl+C** — нормальное завершение, не ошибка.

---

## 7. Автозапуск через systemd

Чтобы бот работал в фоне и перезапускался после перезагрузки сервера.

### 7.1. Создать файл сервиса

```bash
nano /etc/systemd/system/autoscalex.service
```

Вставьте (путь к каталогу замените на свой, если он другой):

```ini
[Unit]
Description=AutoScaleX Pro Telegram Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/tam-sam/AutoScaleX Pro 2.2
ExecStart=/root/tam-sam/AutoScaleX Pro 2.2/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Сохраните: **Ctrl+O**, **Enter**, **Ctrl+X**.

**Важно:** если папка называется `AutoScaleX Pro 2.2` (с пробелами), в `WorkingDirectory` и `ExecStart` путь должен быть именно таким, в кавычки брать не нужно в unit-файле.

### 7.2. Включить и запустить сервис

```bash
systemctl daemon-reload
systemctl enable autoscalex
systemctl start autoscalex
```

### 7.3. Проверить статус

```bash
systemctl status autoscalex
```

Должно быть: **active (running)**. После этого можно закрыть SSH — бот продолжит работать.

---

## Полезные команды

| Действие              | Команда |
|-----------------------|--------|
| Статус бота           | `systemctl status autoscalex` |
| Остановить            | `systemctl stop autoscalex`   |
| Запустить             | `systemctl start autoscalex`  |
| Перезапустить         | `systemctl restart autoscalex` |
| Логи в реальном времени | `journalctl -u autoscalex -f` |
| Последние 100 строк логов | `journalctl -u autoscalex -n 100` |

---

## Пути по умолчанию

При установке в `/root/tam-sam/` бот создаёт данные рядом с каталогом `AutoScaleX Pro 2.2`:

- `user_states` — состояние пользователей
- `user_data` — зашифрованные API ключи
- `referrals.json` / `pending_referrals.json` — рефералы
- `logs` — логи (если включено в конфиге)

Каталог с ботом: `/root/tam-sam/AutoScaleX Pro 2.2`, venv: `/root/tam-sam/AutoScaleX Pro 2.2/venv`.

---

## Обновление бота на сервере

Когда в репозитории на GitHub появились новые изменения (после `git push` с локального ПК), обновите бота на VPS так:

### 1. Подключиться к серверу

```bash
ssh root@IP_ВАШЕГО_СЕРВЕРА
```

### 2. Перейти в каталог репозитория

```bash
cd /root/tam-sam
```

(Не в `AutoScaleX Pro 2.2` — git должен видеть корень репо.)

### 3. Остановить бота

```bash
systemctl stop autoscalex
```

### 4. Подтянуть изменения с GitHub

```bash
git fetch origin
git pull origin main
```

### 5. (По желанию) Проверить, что обновилось

```bash
git log -1 --oneline
```

Должен быть последний коммит (например `e260bbc fix: защита от grid_step 0.65%...`).

### 6. Запустить бота

```bash
systemctl start autoscalex
```

### 7. Проверить, что бот работает

```bash
systemctl status autoscalex
```

Должно быть **active (running)**. Логи в реальном времени:

```bash
journalctl -u autoscalex -f
```

Выход: **Ctrl+C**.

---

**Кратко (скопировать целиком):**

```bash
ssh root@IP_ВАШЕГО_СЕРВЕРА
cd /root/tam-sam
systemctl stop autoscalex
git pull origin main
systemctl start autoscalex
systemctl status autoscalex
```
