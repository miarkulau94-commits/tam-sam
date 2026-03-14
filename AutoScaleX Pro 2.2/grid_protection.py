"""
Защита сетки и подготовка к ребалансу: отмена N BUY, добавление до 5 BUY внизу при 3 BUY.
Логика вынесена из TradingBot (см. GRID_PROTECTION.md).
"""
from __future__ import annotations

import asyncio
import logging
from decimal import ROUND_DOWN, Decimal

from order_manager import Order

log = logging.getLogger("grid_protection")


async def cancel_last_n_buy_orders(bot: "TradingBot", n: int) -> int:
    """Отменить последние N BUY ордеров (самые низкие по цене) для освобождения USDT."""
    try:
        open_buy_orders = [o for o in bot.orders if o.side == "BUY" and o.status == "open"]

        if len(open_buy_orders) == 0:
            log.warning("[CANCEL_LAST_BUY] No open BUY orders to cancel")
            return 0

        open_buy_orders_sorted = sorted(open_buy_orders, key=lambda x: x.price)
        orders_to_cancel = open_buy_orders_sorted[:n]

        log.info(
            f"[CANCEL_LAST_BUY] Cancelling {len(orders_to_cancel)} last BUY orders (lowest prices): {[f'{o.price:.8f}' for o in orders_to_cancel]}"
        )

        canceled_count = 0
        for order in orders_to_cancel:
            try:
                await bot.ex._request("GET", "/openApi/spot/v1/trade/cancel", {"symbol": bot.symbol, "orderId": order.order_id})
                if order in bot.orders:
                    bot.orders.remove(order)
                canceled_count += 1
                log.info(f"[CANCEL_LAST_BUY] ✅ Cancelled BUY order {order.order_id} at {order.price:.8f}")
                await asyncio.sleep(0.1)
            except Exception as e:
                log.warning(f"[CANCEL_LAST_BUY] Failed to cancel BUY order {order.order_id}: {e}")

        log.info(f"[CANCEL_LAST_BUY] Successfully cancelled {canceled_count} out of {len(orders_to_cancel)} BUY orders")
        return canceled_count
    except Exception as e:
        log.error(f"[CANCEL_LAST_BUY] Error cancelling last BUY orders: {e}", exc_info=True)
        return 0


async def check_protection_add_five_buy_when_three_left(bot: "TradingBot") -> int:
    """Защита: при ≤3 открытых BUY и большой сетке добавить до 5 BUY внизу. Порог: 1.5% → 62, 0.75% → 127."""
    try:
        open_buy = [o for o in bot.orders if o.side == "BUY" and o.status == "open"]
        open_buy_count = len(open_buy)
        if open_buy_count > 3:
            return 0
        total_open = len([o for o in bot.orders if o.status == "open"])
        threshold = bot.get_min_open_orders_for_protection()
        if total_open <= threshold:
            log.debug(
                "[PROTECTION_3_BUY] Skip: total_open=%s <= threshold=%s",
                total_open,
                threshold,
            )
            return 0
        current_price = await bot.get_current_price()
        log.info(
            "[PROTECTION_3_BUY] open_buy=%s, total_open=%s > threshold=%s -> adding up to 5 BUY at bottom",
            open_buy_count,
            total_open,
            threshold,
        )
        return await create_buy_orders_at_bottom(bot, current_price)
    except Exception as e:
        log.error("[PROTECTION_3_BUY] Error: %s", e, exc_info=True)
        return 0


async def create_buy_orders_at_bottom(bot: "TradingBot", current_price: Decimal) -> int:
    """Создать BUY ордера внизу сетки (ниже всех существующих BUY). Возвращает количество созданных."""
    try:
        info = await bot.ex.symbol_info(bot.symbol)
        step = info.get("stepSize", Decimal("0.000001"))
        tick = info.get("tickSize", Decimal("0.01"))
        min_qty = info.get("minQty", Decimal("0.000001"))
        min_notional = info.get("minNotional", Decimal("0"))

        open_buy_orders = [o for o in bot.orders if o.side == "BUY" and o.status == "open"]

        if open_buy_orders:
            lowest_buy_price = min(o.price for o in open_buy_orders)
            start_price = lowest_buy_price * (Decimal("1") - bot.grid_step_pct)
            log.info(f"[CREATE_BUY_AT_BOTTOM] Starting from price {start_price:.8f} (lowest existing BUY: {lowest_buy_price:.8f})")
        else:
            start_price = current_price * (Decimal("1") - bot.grid_step_pct)
            log.info(f"[CREATE_BUY_AT_BOTTOM] No existing BUY orders, starting from price {start_price:.8f} (current_price: {current_price:.8f})")

        quote_available = await bot.ex.available_balance(bot.quote_asset_name)
        quote_balance = await bot.ex.balance(bot.quote_asset_name)
        log.info(
            f"[CREATE_BUY_AT_BOTTOM] Balance check: available={quote_available:.2f}, total={quote_balance:.2f}, order_value={bot.buy_order_value:.2f}"
        )

        if quote_available < bot.buy_order_value:
            log.warning(f"[CREATE_BUY_AT_BOTTOM] Insufficient available balance: {quote_available:.2f} < {bot.buy_order_value:.2f}")
            return 0

        max_buy_orders = bot.get_max_buy_orders()
        open_sell_orders = [o for o in bot.orders if o.side == "SELL" and o.status == "open"]
        initial_sell_count = 5
        max_allowed_buy = max_buy_orders + (initial_sell_count - len(open_sell_orders))
        if len(open_buy_orders) >= max_allowed_buy:
            log.debug(
                f"[CREATE_BUY_AT_BOTTOM] BUY limit reached: {len(open_buy_orders)} >= {max_allowed_buy} (open SELL={len(open_sell_orders)})"
            )
            return 0

        max_orders = min(max_allowed_buy - len(open_buy_orders), 5, int(quote_available / bot.buy_order_value))

        created_count = 0
        current_buy_price = start_price
        for i in range(max_orders):
            current_open_buy = len([o for o in bot.orders if o.side == "BUY" and o.status == "open"])
            if current_open_buy >= max_allowed_buy:
                log.debug(f"[CREATE_BUY_AT_BOTTOM] BUY limit reached during loop: {current_open_buy} >= {max_allowed_buy}")
                break
            current_available = await bot.ex.available_balance(bot.quote_asset_name)
            if current_available < bot.buy_order_value:
                log.info(
                    f"[CREATE_BUY_AT_BOTTOM] Insufficient available balance for more orders: {current_available:.2f} < {bot.buy_order_value:.2f}"
                )
                break

            level_price = (current_buy_price // tick) * tick

            if level_price <= 0:
                log.warning(f"[CREATE_BUY_AT_BOTTOM] Price too low: {level_price}, stopping")
                break

            existing_at_level = any(o.side == "BUY" and abs(o.price - level_price) < tick and o.status == "open" for o in bot.orders)
            if existing_at_level:
                log.debug(f"[CREATE_BUY_AT_BOTTOM] Skip level {level_price:.8f} (already has BUY)")
                current_buy_price = current_buy_price * (Decimal("1") - bot.grid_step_pct)
                continue

            qty = (bot.buy_order_value / level_price).quantize(step, rounding=ROUND_DOWN)
            notional = qty * level_price
            required_notional = bot.get_required_notional(min_notional)

            if qty >= min_qty and notional >= required_notional:
                try:
                    result = await bot.ex.place_limit(bot.symbol, "BUY", qty, level_price, delay=0.1)
                    if result and result.get("orderId"):
                        order = Order(
                            order_id=str(result.get("orderId", "")), side="BUY", price=level_price, qty=qty, amount_usdt=bot.buy_order_value
                        )
                        bot.orders.append(order)
                        created_count += 1
                        log.info(f"🟩 [CREATE_BUY_AT_BOTTOM] ✅ Created BUY order at {level_price:.8f}, qty={qty:.8f}")
                        await asyncio.sleep(0.2)
                except Exception as e:
                    log.warning(f"[CREATE_BUY_AT_BOTTOM] Failed to place BUY order at {level_price}: {e}")
            else:
                log.warning(
                    f"[CREATE_BUY_AT_BOTTOM] Validation failed: qty={qty:.8f} (min={min_qty:.8f}), notional={notional:.8f} (required={required_notional:.8f})"
                )

            current_buy_price = current_buy_price * (Decimal("1") - bot.grid_step_pct)

        log.info(f"🟩 [CREATE_BUY_AT_BOTTOM] Created {created_count} BUY orders at bottom of grid")
        return created_count
    except Exception as e:
        log.error(f"[CREATE_BUY_AT_BOTTOM] Error creating BUY orders at bottom: {e}", exc_info=True)
        return 0
