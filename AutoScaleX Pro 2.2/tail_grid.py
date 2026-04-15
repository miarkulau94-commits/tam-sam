"""
Хвост сетки: ATR (Wilder) по свечам 4H, пороги open SELL для авто-VWAP и отмены хвоста.
"""
from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional

import config

log = logging.getLogger("tail_grid")


def open_sell_threshold_for_grid_step(grid_step_pct: Optional[Decimal]) -> int:
    """Порог числа открытых SELL: 0.75% шага → выше порог (120), 1.5% → ниже (60); между — линейно."""
    t_hi = config.TAIL_OPEN_SELL_THRESHOLD_0_75_PCT
    t_lo = config.TAIL_OPEN_SELL_THRESHOLD_1_5_PCT
    if grid_step_pct is None:
        return t_hi
    step = float(grid_step_pct)
    if step <= 0.0075:
        return t_hi
    if step >= 0.015:
        return t_lo
    r = (step - 0.0075) / (0.015 - 0.0075)
    return int(round(t_hi - r * (t_hi - t_lo)))


def should_block_auto_vwap(open_sell_count: int, grid_step_pct: Optional[Decimal]) -> bool:
    """Не создавать авто-SELL сетку от VWAP при «переполненной» стороне SELL."""
    return open_sell_count >= open_sell_threshold_for_grid_step(grid_step_pct)


def should_allow_tail_cancel(open_sell_count: int, grid_step_pct: Optional[Decimal]) -> bool:
    """Разрешить отмену хвостовых BUY только при open SELL не выше порога (анти-дребезг с should_block_auto_vwap)."""
    return open_sell_count <= open_sell_threshold_for_grid_step(grid_step_pct)


def normalize_klines_payload(raw: Any) -> List[Dict[str, Any]]:
    """Преобразовать ответ get_spot_klines_v2 в список свечей {open,high,low,close}."""
    if raw is None:
        return []
    data = raw
    if isinstance(data, dict):
        if isinstance(data.get("data"), list):
            data = data["data"]
        elif isinstance(data.get("klines"), list):
            data = data["klines"]
        else:
            return []
    if not isinstance(data, list):
        return []
    out: List[Dict[str, Any]] = []
    for row in data:
        try:
            if isinstance(row, dict):
                o = row.get("open") or row.get("o")
                h = row.get("high") or row.get("h")
                lo = row.get("low") or row.get("l")
                c = row.get("close") or row.get("c")
                t_raw = row.get("time") or row.get("t") or row.get("openTime")
            elif isinstance(row, (list, tuple)) and len(row) >= 5:
                t_raw = row[0]
                o, h, lo, c = row[1], row[2], row[3], row[4]
            else:
                continue
            candle: Dict[str, Any] = {
                "open": Decimal(str(o)),
                "high": Decimal(str(h)),
                "low": Decimal(str(lo)),
                "close": Decimal(str(c)),
            }
            if t_raw is not None:
                try:
                    candle["_t"] = int(t_raw)
                except (TypeError, ValueError):
                    pass
            out.append(candle)
        except Exception:
            continue
    return out


def order_candles_chronologically(candles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Старые → новые для ATR. Сортировка по time; иначе предполагаем порядок newest-first и разворачиваем."""
    if not candles:
        return candles
    if any("_t" in c for c in candles):
        return sorted(candles, key=lambda x: x.get("_t", 0))
    return list(reversed(candles))


def compute_atr_wilder(candles_oldest_first: List[Dict[str, Any]], period: int) -> Optional[Decimal]:
    """ATR Wilder по True Range. candles — по возрастанию времени."""
    if period < 1 or len(candles_oldest_first) < period + 1:
        return None
    trs: List[Decimal] = []
    for i in range(1, len(candles_oldest_first)):
        h = candles_oldest_first[i]["high"]
        l = candles_oldest_first[i]["low"]
        pc = candles_oldest_first[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / Decimal(period)
    for j in range(period, len(trs)):
        atr = (atr * Decimal(period - 1) + trs[j]) / Decimal(period)
    return atr


def step_tail_price_wilder(
    atr: Decimal,
    k: Decimal,
    tick: Decimal,
    fallback_price_distance: Decimal,
) -> Decimal:
    """ТЗ п.4.2: step_tail = round_to_tick(ATR × k) в единицах цены; при ATR≤0 — fallback (шаг от основы)."""
    raw = (atr * k) if atr is not None and atr > 0 else fallback_price_distance
    if raw <= 0:
        raw = fallback_price_distance
    if not tick or tick <= 0:
        return raw
    n = (raw / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return n * tick
