# Хвост сетки (ATR), авто/ручной VWAP и классический ребаланс

Документ описывает поведение по ТЗ на хвост сетки и правила VWAP: какие сценарии что вызывают, **пороги по числу открытых SELL**, логи и настройки. Стороны ордеров в коде и на бирже — **BUY** / **SELL** (как в API); ниже они сохраняются так же.

> Не подменяйте этот файл автопереводом с английского: термины и имена полей исказятся (как «КУПИТЬ» вместо BUY, «пороги открываются» и т.п.).

См. также: `tail_grid.py`, `trading_bot.py` (`try_activate_tail_grid`, `cancel_tail_buy_orders_if_allowed`, `maybe_process_tail_grid`, `create_critical_sell_grid`), `rebalance.py`, `handlers.py`, `telegram_bot.py` (ребаланс SELL от VWAP).

Матрица сценариев и регрессионные проверки: `tests/test_vwap_tail_matrix.py`, `tests/test_tail_grid.py`.

---

## 1. Базовая сетка и хвост

- **Основа:** 5 SELL + N BUY, где N зависит от шага (`get_max_buy_orders()`): при **1.5%** — до **60** BUY, при **0.75%** — до **125** BUY (между шагами — линейная интерполяция).
- **Базовая лестница:** у каждого BUY основы при выставлении задаётся `base_ladder_index` (1…N). Исполнение такого ордера добавляет индекс в `_base_ladder_filled_indices` и обновляет `_last_base_buy_fill_price` (якорь для хвоста).
- **Хвост:** дополнительные лимитные BUY **ниже** основы, метка `is_tail=True`, отдельные поля в state. Шаг между уровнями хвоста — **`step_tail`** в **единицах цены**: `round_to_tick(ATR × k)` по свечам **4H**, ATR(14) Wilder; при необходимости fallback от `grid_step_pct × цена`. Запрос **spot v2 kline** используется **только** для расчёта ATR при включении/перевключении хвоста.

---

## 2. Когда включается хвост

**Триггер:** все уровни базовой лестницы исполнены: множество `{1,…,N}` ⊆ `_base_ladder_filled_indices`, и ещё не была завершена активация этой «волны» (`_tail_activation_done` сбрасывается при отмене хвоста или новой сетке).

**Не блокируется** числом открытых SELL на бирже: после съедения базы при большом количестве открытых SELL хвост всё равно может активироваться (как в ТЗ п.4).

**Где вызывается:** `maybe_process_tail_grid()` → `try_activate_tail_grid(price)` после `cancel_tail_buy_orders_if_allowed()`.

**Точки входа:** основной цикл TRADING (после `check_orders` / `sync` / `check_critical_situation`) и `handle_buy_filled` после `check_rebalancing_after_all_buy_filled`.

---

## 3. Анти-дребезг перевключения хвоста (ТЗ п.4.6)

Между **повторным** запросом kline / новой волной хвоста действует кулдаун после:

- успешного запроса **`get_spot_klines_v2`** для хвоста;
- фактической **отмены** хвостовых BUY в `cancel_tail_buy_orders_if_allowed`.

**По умолчанию:** **900 секунд (15 минут)**. Переменная окружения: **`TAIL_ANTIFLAP_COOLDOWN_SEC`** (`0` — выключить). Время последнего события хранится в **`tail_antiflap_last_ts`** (state).

---

## 4. Отмена хвоста (ТЗ п.8)

Отменяются только **открытые BUY с `is_tail=True`**, порядок: **с минимальной цены вверх**.

**Условие:** число открытых **SELL** на бирже (предпочтительно `open_orders`) **не больше** порога для шага: **60** при 1.5%, **120** при 0.75% (между — интерполяция, `tail_grid.open_sell_threshold_for_grid_step`). Если SELL **строго больше** порога — отмена не выполняется (анти-дребезг).

---

## 5. Автоматический SELL от VWAP (ТЗ п.9.1)

**Где:** `rebalance.check_rebalancing_after_all_buy_filled` — когда на бирже и в памяти **нет открытых BUY**, состояние TRADING, VWAP > 0.

**Блокировка:** если число открытых **SELL по ответу биржи** ≥ порога (те же **60 / 120**), вызов **`create_critical_sell_grid(vwap_source="auto_after_all_buy")`** **не выполняется** — чтобы не наращивать узкую критическую SELL-сетку от VWAP при уже «переполненной» стороне SELL.

**Классический ребаланс при 0 SELL** (`check_rebalancing`) **не** использует этот порог и **не** блокируется флагом хвоста (ТЗ п.10).

---

## 6. Ручной SELL от VWAP (ТЗ п.9.2)

Кнопка в Telegram **«Ребалансировать SELL»** вызывает **`create_critical_sell_grid(vwap_source="manual_telegram")`** — **всегда**, независимо от числа открытых SELL и `tail_active`.

---

## 7. Критическая SELL-сетка от VWAP (депозит)

При срабатывании **`check_critical_level`** (все BUY исполнены, лимит по количеству, запрос депозита) вызывается **`create_critical_sell_grid(vwap_source="critical_level")`**.

---

## 8. Логирование VWAP-сетки (ТЗ п.12)

Все сценарии построения «критической» SELL-сетки от VWAP пишут в лог префикс **`[VWAP_GRID]`** и поле **`source=`**:

| `vwap_source`        | Откуда |
|----------------------|--------|
| `manual_telegram`    | Telegram, ручной ребаланс SELL |
| `auto_after_all_buy` | Авто после исчезновения всех открытых BUY |
| `critical_level`     | Критический уровень / депозит |
| `auto`               | Устаревший вызов без явной метки (по умолчанию) |

---

## 9. Переменные окружения (хвост и пороги)

Задаются в `.env` или через систему окружения; см. `config.py`.

| Переменная | Назначение |
|------------|------------|
| `TAIL_ATR_PERIOD` | Период ATR (по умолчанию 14) |
| `TAIL_ATR_INTERVAL` | Интервал свечей API (по умолчанию `4h`) |
| `TAIL_ATR_KLINE_LIMIT` | Число свечей для запроса |
| `TAIL_ATR_MULTIPLIER_K` | Множитель k для `ATR × k` |
| `TAIL_MAX_ORDERS` | Максимум хвостовых BUY за волну (по умолчанию 30) |
| `TAIL_OPEN_SELL_THRESHOLD_1_5_PCT` | Порог open SELL для шага 1.5% (по умолчанию 60) |
| `TAIL_OPEN_SELL_THRESHOLD_0_75_PCT` | Порог для 0.75% (по умолчанию 120) |
| `TAIL_ANTIFLAP_COOLDOWN_SEC` | Анти-дребезг, сек (по умолчанию 900; `0` — выкл.) |

---

## 10. Поля state, связанные с хвостом

`tail_active`, `tail_order_ids`, `step_tail`, `tail_anchor_price`, `tail_activated_at`, `tail_activation_done`, `last_base_buy_fill_price`, `base_ladder_count`, `base_ladder_filled_indices`, `tail_antiflap_last_ts`.

---

*Документ актуален для AutoScaleX Pro 2.2.*
