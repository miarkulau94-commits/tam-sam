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
load_dotenv(os.path.join(_BASE_DIR, "..", ".env"))  # корень репозитория (на уровень выше папки бота)
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

# Критический уровень / ребаланс SELL: три уровня цены над VWAP (%, одинаково для авто и ручного ребаланса)
CRITICAL_SELL_PROFIT_PCT = [Decimal("1.5"), Decimal("2.5"), Decimal("3.5")]
CRITICAL_SELL_DIVISIONS = len(CRITICAL_SELL_PROFIT_PCT)

# BingX настройки (sandbox не используется — всегда production API)
MIN_ORDER = Decimal(os.getenv("MIN_ORDER", "20"))

# BingX HTTP (requests.Session): connect — установка TCP+TLS; read — ожидание тела ответа.
# В .env: BINGX_HTTP_CONNECT_TIMEOUT=20 и BINGX_HTTP_READ_TIMEOUT=20 (секунды, можно дробные).
def _positive_http_timeout(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return default
        v = float(raw)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


BINGX_HTTP_CONNECT_TIMEOUT = _positive_http_timeout("BINGX_HTTP_CONNECT_TIMEOUT", 20.0)
BINGX_HTTP_READ_TIMEOUT = _positive_http_timeout("BINGX_HTTP_READ_TIMEOUT", 20.0)

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

# --- Хвост сетки (ATR 4H): только для включения/перевключения хвоста ---
TAIL_ATR_PERIOD = int(os.getenv("TAIL_ATR_PERIOD", "14"))
TAIL_ATR_INTERVAL = os.getenv("TAIL_ATR_INTERVAL", "4h")  # BingX kline interval
TAIL_ATR_KLINE_LIMIT = int(os.getenv("TAIL_ATR_KLINE_LIMIT", "50"))  # свечей для расчёта (≥ period+1)
TAIL_ATR_MULTIPLIER_K = Decimal(os.getenv("TAIL_ATR_MULTIPLIER_K", "0.5"))
TAIL_MAX_ORDERS = int(os.getenv("TAIL_MAX_ORDERS", "30"))
# Пороги open SELL: авто-VWAP не глушить ниже этого; отмена хвоста только при open_SELL ≤ порога
TAIL_OPEN_SELL_THRESHOLD_1_5_PCT = int(os.getenv("TAIL_OPEN_SELL_THRESHOLD_1_5_PCT", "60"))
TAIL_OPEN_SELL_THRESHOLD_0_75_PCT = int(os.getenv("TAIL_OPEN_SELL_THRESHOLD_0_75_PCT", "120"))
# ТЗ п.4.6: анти-дребезг — после отмены хвоста или после запроса kline для хвоста не чаще чем раз в N секунд (0 = выкл.)
TAIL_ANTIFLAP_COOLDOWN_SEC = int(os.getenv("TAIL_ANTIFLAP_COOLDOWN_SEC", str(15 * 60)))

# Защита Profit Bank: не зачислять в profit_bank прибыль выше этой с одной SELL (защита от ошибочного/призрачного расчёта при пустых FIFO-позициях)
# 9 USDT — при минимальных ордерах от 10 USDT нормальная прибыль с одной SELL всегда меньше
PROFIT_BANK_MAX_PROFIT_PER_SELL = Decimal(os.getenv("PROFIT_BANK_MAX_PROFIT_PER_SELL", "9"))

# --- Поиск свободного уровня после fill (гибрид: сетка + «мелкие» отступы от якоря) ---
# Подробно: GRID_FREE_LEVELS.md
# Сколько раз идти вниз/вверх строго по grid_step_pct, прежде чем пробовать мелкие % от якоря
GRID_FREE_MAX_STEPS = int(os.getenv("GRID_FREE_MAX_STEPS", "5"))
# Мелкие отступы от якорной цены (доля, не %): только fb < основного шага; после исчерпания GRID_FREE_MAX_STEPS
GRID_FALLBACK_BUY_BELOW_ANCHOR_PCT_015 = [
    Decimal("0.014"),
    Decimal("0.0125"),
    Decimal("0.01"),
    Decimal("0.008"),
    Decimal("0.005"),
]
GRID_FALLBACK_BUY_BELOW_ANCHOR_PCT_0075 = [
    Decimal("0.006"),
    Decimal("0.005"),
    Decimal("0.004"),
    Decimal("0.003"),
]
# Для произвольного шага сетки: перебираются только значения строго меньше grid_step_pct
GRID_FALLBACK_BELOW_ANCHOR_PCT_GENERIC = [
    Decimal("0.014"),
    Decimal("0.0125"),
    Decimal("0.01"),
    Decimal("0.008"),
    Decimal("0.006"),
    Decimal("0.005"),
    Decimal("0.004"),
    Decimal("0.003"),
]

# Синхронизация ордеров: максимум запросов get_order за один sync (остальные — путь как в check_orders, без get_order)
SYNC_GET_ORDER_MAX_PER_CALL = int(os.getenv("SYNC_GET_ORDER_MAX_PER_CALL", "10"))
# Экран «Баланс» в Telegram: лимит get_order за один sync (меньше нагрузки при просмотре)
SYNC_BALANCE_MAX_GET_ORDER = int(os.getenv("SYNC_BALANCE_MAX_GET_ORDER", "3"))
