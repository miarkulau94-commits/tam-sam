"""
Конфигурация AutoScaleX Pro 2.2.

Загружает переменные из .env (приоритет: cwd, ../.env, AutoScaleX Pro 2.2/.env).
Ключевые параметры: TG_TOKEN, ENCRYPTION_SECRET, BINGX_*, SYMBOL, FEE_RATE.
"""
import os
import logging
from decimal import Decimal
from dotenv import load_dotenv

# Загрузка .env: сначала общие, затем .env в папке скрипта (наивысший приоритет)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv()  # cwd
load_dotenv(os.path.join(_BASE_DIR, "..", ".env"))  # nrq
load_dotenv(os.path.join(_BASE_DIR, ".env"))  # AutoScaleX Pro 2.2 — приоритет

log = logging.getLogger("config")

# API настройки
API_KEY = os.getenv("BINGX_API_KEY", "")
SECRET = os.getenv("BINGX_SECRET", "")
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_ADMIN_ID = int(os.getenv("TG_ADMIN_ID") or "0")  # ID администратора (пустая строка = 0)

# Шифрование API ключей. ОБЯЗАТЕЛЬНО в .env — без него бот не запустится.
ENCRYPTION_SECRET = os.getenv("ENCRYPTION_SECRET", "")

# Торговая пара (с валидацией формата)
SYMBOL_RAW = os.getenv("SYMBOL", "ETH-USDT")
if "-" in SYMBOL_RAW:
    SYMBOL = SYMBOL_RAW
    BASE, QUOTE = SYMBOL.split("-")
else:
    if SYMBOL_RAW and SYMBOL_RAW != "ETH-USDT":
        log.warning(f"Invalid SYMBOL format '{SYMBOL_RAW}' (expected BASE-QUOTE), using ETH-USDT")
    SYMBOL = "ETH-USDT"
    BASE, QUOTE = "ETH", "USDT"

# Параметры стратегии
GRID_STEP_PCT = Decimal("0.0075")  # 0.75% по умолчанию
BUY_ORDER_VALUE = Decimal("50")
MIN_BUY_ORDERS = 1
MAX_BUY_ORDERS = 125  # BUY макс при 0.75% (всего 125+5=130)
SELL_ORDERS_COUNT = 5
# Комиссия биржи (0.001 = 0.1%). Можно переопределить через FEE_RATE в .env
try:
    _fee = os.getenv("FEE_RATE", "0.001")
    FEE_RATE = Decimal(str(_fee)) if _fee else Decimal("0.001")
except Exception:
    FEE_RATE = Decimal("0.001")
BASE_DEPOSIT = Decimal("1000")

# Критический уровень
CRITICAL_GRID_STEP_MULTIPLIER = [2, 4, 6]
CRITICAL_SELL_DIVISIONS = 3

# BingX настройки (sandbox не используется — всегда production API)
MIN_ORDER = Decimal(os.getenv("MIN_ORDER", "20"))

# Реферальная система
REFERRAL_LINK = os.getenv("REFERRAL_LINK", "https://iciclebridge.com/invite/GJCRMN/")
REFERRALS_FILE = os.path.normpath(os.path.join(_BASE_DIR, "..", "referrals.json"))

# Сохранение состояния (абсолютные пути)
STATE_DIR = os.path.normpath(os.path.join(_BASE_DIR, "..", "user_states"))
USER_DATA_DIR = os.path.normpath(os.path.join(_BASE_DIR, "..", "user_data"))
TRADES_DIR = os.path.normpath(os.path.join(_BASE_DIR, "..", "trades"))

# Логирование
LOG_DIR = os.path.normpath(os.path.join(_BASE_DIR, "..", "logs"))
# Вывод логов в консоль (false — только в файл, удобно для Docker/systemd)
CONSOLE_LOG = os.getenv("CONSOLE_LOG", "true").lower() in ("true", "1", "yes")

# BingX minVolume для ETH/BTC (выше minQty из API)
MIN_QTY_FLOOR_ETH_BTC = Decimal(os.getenv("MIN_QTY_FLOOR_ETH_BTC", "0.0004"))

# Троттлинг критических уведомлений в Telegram (секунды между сообщениями на пользователя)
ERROR_NOTIFY_COOLDOWN = int(os.getenv("ERROR_NOTIFY_COOLDOWN", "1800"))

# Максимум одновременных перестроек SELL-сетки (остальные ждут в очереди)
REBALANCING_SEMAPHORE_LIMIT = int(os.getenv("REBALANCING_SEMAPHORE_LIMIT", "15"))

# --- Мониторинг API (логирование метрик, алерт админу) ---
API_METRICS_LOG_INTERVAL_SEC = int(os.getenv("API_METRICS_LOG_INTERVAL_SEC", "120"))  # раз в 2 мин
API_METRICS_ERROR_ALERT_THRESHOLD = int(os.getenv("API_METRICS_ERROR_ALERT_THRESHOLD", "10"))  # алерт в TG при ошибках >= N за 60 с

# --- Константы сценариев (защита сетки, подготовка к ребалансу) ---
# При 1 оставшемся SELL отменяем столько самых низких BUY, чтобы освободить USDT под рыночную покупку
REBALANCE_PREP_CANCEL_BUY_COUNT = 5
# Порог открытых ордеров для защиты «3 BUY → добавить до 5 BUY внизу»: только при «большой» сетке
PROTECTION_THRESHOLD_1_5_PCT = 62   # шаг сетки 1.5%
PROTECTION_THRESHOLD_0_75_PCT = 127  # шаг сетки 0.75%
