"""
Обработчики исполнения ордеров (BUY/SELL fill).
Логика вынесена из TradingBot для уменьшения размера trading_bot.py и удобства тестирования.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import ROUND_HALF_UP, Decimal

import config
from order_manager import Order
from structured_logging import set_log_context

log = logging.getLogger("handlers")


async def handle_buy_filled(bot: "TradingBot", order: Order, price: Decimal) -> None:
    """Обработка исполнения BUY ордера."""
    from trading_bot import BotState

    set_log_context(user_id=bot.user_id, symbol=bot.symbol, order_id=order.order_id)
    try:
        if bot.state == BotState.STOPPED:
            return
        log.info(f"🟩 Processing BUY order fill: orderId={order.order_id}, price={price:.8f}, qty={order.qty:.8f}")

        bot.base_asset = await bot.ex.balance(bot.base_asset_name)
        bot.current_deposit = await bot.ex.balance(bot.quote_asset_name)

        btc_received = order.qty * (Decimal("1") - config.FEE_RATE)
        bot.total_executed_buys += 1

        log.info(
            f"🟩 BUY executed: received={btc_received:.8f} {bot.base_asset_name}, balance={bot.base_asset:.8f}, deposit={bot.current_deposit:.2f} {bot.quote_asset_name}"
        )

        bot.position_manager.add_position(price, btc_received)
        order.status = "filled"
        order.executed_qty = order.qty

        bot.statistics.save_trade(
            {
                "type": "BUY",
                "symbol": bot.symbol,
                "price": price,
                "qty": btc_received,
                "amount_usdt": order.amount_usdt if order.amount_usdt > 0 else (order.qty * price),
                "profit": Decimal("0"),
                "profit_bank": bot.profit_bank,
                "total_equity": await bot.get_total_equity(price),
            }
        )

        log.info(f"[BUY FILL] Step 1: Attempting to create SELL order after BUY fill at price {price:.8f}")
        try:
            info = await bot.ex.symbol_info(bot.symbol)
            step = info["stepSize"]
            tick = info["tickSize"]
            min_qty = info.get("minQty", Decimal("0.000001"))
            min_notional = info.get("minNotional", Decimal("0"))

            sell_price = price * (Decimal("1") + bot.grid_step_pct)
            if tick > 0:
                sell_price = (sell_price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick
            else:
                sell_price = (sell_price // tick) * tick
            log.info(f"[CREATE_SELL_AFTER_BUY] Calculated SELL price: {price:.8f} * (1 + {bot.grid_step_pct:.6f}) = {sell_price:.8f}")

            await bot.ex.invalidate_balance_cache(bot.base_asset_name)
            await asyncio.sleep(0.35)
            current_base_balance = await bot.ex.balance(bot.base_asset_name)
            available_base_asset = await bot.ex.available_balance(bot.base_asset_name)

            # Парный SELL к исполненному BUY: объём = полученное с этой сделки (после комиссии), округление вниз к step.
            # Не использовать min(available): при почти всём KSM в открытых SELL free≈0, min давал «пыль» ниже minVolume биржи.
            sell_qty = (btc_received // step) * step

            if sell_qty > available_base_asset:
                await bot.ex.invalidate_balance_cache(bot.base_asset_name)
                await asyncio.sleep(0.5)
                current_base_balance = await bot.ex.balance(bot.base_asset_name)
                available_base_asset = await bot.ex.available_balance(bot.base_asset_name)

            log.info(
                f"[CREATE_SELL_AFTER_BUY] Balance check: total={current_base_balance:.8f}, available(free)={available_base_asset:.8f}, btc_received={btc_received:.8f}"
            )
            log.info(
                f"[CREATE_SELL_AFTER_BUY] Qty calculation: btc_received={btc_received:.8f}, available={available_base_asset:.8f}, sell_qty={sell_qty:.8f} (hedge from fill, not capped by dust)"
            )

            existing_sell = any(o.side == "SELL" and abs(o.price - sell_price) < tick and o.status == "open" for o in bot.orders)

            if existing_sell:
                log.warning(f"[CREATE_SELL_AFTER_BUY] SKIPPED: SELL order already exists at price {sell_price:.8f}")
            elif sell_qty < min_qty:
                log.warning(
                    f"[CREATE_SELL_AFTER_BUY] FAILED: SELL order qty too small: {sell_qty:.8f} < {min_qty:.8f} (min_qty), available_base={available_base_asset:.8f}, btc_received={btc_received:.8f}"
                )
            else:
                sell_notional = sell_qty * sell_price
                required_notional = bot.get_required_notional(min_notional)
                log.info(
                    f"[CREATE_SELL_AFTER_BUY] Validation: qty={sell_qty:.8f}, price={sell_price:.8f}, notional={sell_notional:.8f}, required={required_notional:.8f}"
                )

                if sell_notional < required_notional:
                    log.warning(f"[CREATE_SELL_AFTER_BUY] FAILED: SELL order notional too small: {sell_notional:.8f} < {required_notional:.8f}")
                elif available_base_asset < sell_qty:
                    log.warning(
                        f"[CREATE_SELL_AFTER_BUY] SKIPPED: free {available_base_asset:.8f} < hedge SELL qty {sell_qty:.8f} "
                        f"(total={current_base_balance:.8f}). If assets are locked in open SELLs, cancel some on the exchange."
                    )
                else:
                    if bot.state == BotState.STOPPED:
                        return
                    try:
                        log.info(f"[CREATE_SELL_AFTER_BUY] Placing SELL order: price={sell_price:.8f}, qty={sell_qty:.8f}")
                        result = await bot.ex.place_limit(bot.symbol, "SELL", sell_qty, sell_price, delay=0.1)
                        if result and result.get("orderId"):
                            sell_order = Order(order_id=str(result.get("orderId", "")), side="SELL", price=sell_price, qty=sell_qty)
                            bot.orders.append(sell_order)
                            log.info(
                                f"🟥 ✅ [CREATE_SELL_AFTER_BUY] SUCCESS: Created SELL order after BUY fill: orderId={result.get('orderId')}, price={sell_price:.8f}, qty={sell_qty:.8f}, amount={sell_notional:.2f} {bot.quote_asset_name}"
                            )
                        elif result:
                            log.warning(f"[CREATE_SELL_AFTER_BUY] FAILED: API returned result without orderId: {result}")
                        else:
                            log.warning("[CREATE_SELL_AFTER_BUY] FAILED: API returned None or empty result")
                    except Exception as e:
                        log.error(f"[CREATE_SELL_AFTER_BUY] EXCEPTION: Failed to place SELL order at {sell_price:.8f}: {e}", exc_info=True)
        except Exception as e:
            log.error(f"[CREATE_SELL_AFTER_BUY] EXCEPTION: Failed to create SELL order after BUY fill: {e}", exc_info=True)

        await asyncio.sleep(1.5)
        log.info("[BUY FILL] Step 2: Syncing orders from exchange")
        await bot.sync_orders_from_exchange()

        open_sell_after = [o for o in bot.orders if o.side == "SELL" and o.status == "open"]
        open_buy_after = [o for o in bot.orders if o.side == "BUY" and o.status == "open"]
        log.info(f"[BUY FILL] After sync: open SELL={len(open_sell_after)}, open BUY={len(open_buy_after)}")

        log.info("[BUY FILL] Step 3: Checking rebalancing after BUY fill")
        await bot.check_rebalancing_after_all_buy_filled(price)

        if len(open_sell_after) >= 3 and getattr(bot, "_cancelled_buy_for_rebalance_prep", False):
            log.info(
                f"[BUY FILL] SELL >= 3 ({len(open_sell_after)}), restoring BUY at bottom (price went down after cancel)"
            )
            buy_restored = await bot.create_buy_orders_at_bottom(price)
            if buy_restored > 0:
                log.info(f"[BUY FILL] Restored {buy_restored} BUY orders at bottom of grid")
            bot._cancelled_buy_for_rebalance_prep = False

        log.info(f"🟩 [BUY FILL] Processing completed for order {order.order_id}")

        await bot.check_critical_level(price)
        if bot.profit_bank > 0:
            await bot.check_pyramiding()

        await asyncio.to_thread(bot.save_state)

    except Exception as e:
        log.error(f"Error handling BUY fill: {e}", exc_info=True)


async def handle_sell_filled(bot: "TradingBot", order: Order, price: Decimal) -> None:
    """Обработка исполнения SELL ордера."""
    from trading_bot import BotState

    set_log_context(user_id=bot.user_id, symbol=bot.symbol, order_id=order.order_id)
    try:
        if bot.state == BotState.STOPPED:
            return
        log.info("🟥 ✅ [SELL FILL] ========================================")
        log.info(f"🟥 ✅ [SELL FILL] Processing SELL order fill: orderId={order.order_id}, price={price:.8f}, qty={order.qty:.8f}")
        log.info("🟥 ✅ [SELL FILL] ========================================")

        profit = bot.position_manager.calculate_profit_for_sell(order.qty, price, config.FEE_RATE)

        bot.base_asset = await bot.ex.balance(bot.base_asset_name)
        bot.current_deposit = await bot.ex.balance(bot.quote_asset_name)

        # Profit bank: копится только положительная прибыль по сделке; убытки в банк не зачисляются (не уходит в минус).
        # Защита: не зачислять аномально большую прибыль с одной SELL (ошибка расчёта при пустых/неверных FIFO-позициях)
        if profit > config.PROFIT_BANK_MAX_PROFIT_PER_SELL:
            log.warning(
                "[SELL FILL] Profit %.2f exceeds PROFIT_BANK_MAX_PROFIT_PER_SELL (%s), not adding to profit_bank (recorded in statistics only)",
                profit,
                config.PROFIT_BANK_MAX_PROFIT_PER_SELL,
            )
            profit_for_bank = Decimal("0")
        elif profit > 0:
            profit_for_bank = profit
        else:
            profit_for_bank = Decimal("0")

        bot.profit_bank += profit_for_bank
        if bot.profit_bank < 0:
            bot.profit_bank = Decimal("0")
        bot.total_executed_sells += 1

        order.status = "filled"
        order.executed_qty = order.qty

        bot.statistics.save_trade(
            {
                "type": "SELL",
                "symbol": bot.symbol,
                "price": price,
                "qty": order.qty,
                "amount_usdt": order.qty * price,
                "profit": profit,
                "profit_bank": bot.profit_bank,
                "total_equity": await bot.get_total_equity(price),
            }
        )

        log.info(f"🟥 SELL trade saved: profit={profit:.8f}, profit_bank={bot.profit_bank:.8f}, total_executed_sells={bot.total_executed_sells}")

        await bot.ex.invalidate_balance_cache(bot.quote_asset_name)
        await asyncio.sleep(0.5)

        log.info(f"[SELL FILL] Step 1: Calling create_buy_after_sell with price {price:.8f}")
        buy_created = await bot.create_buy_after_sell(price)
        log.info(f"[SELL FILL] Step 1 result: buy_created={buy_created}")

        await asyncio.sleep(1.5)
        log.info("[SELL FILL] Step 2: Syncing orders from exchange")
        await bot.sync_orders_from_exchange()

        open_sell_after = [o for o in bot.orders if o.side == "SELL" and o.status == "open"]
        open_buy_after = [o for o in bot.orders if o.side == "BUY" and o.status == "open"]
        log.info(f"[SELL FILL] After sync: open SELL={len(open_sell_after)}, open BUY={len(open_buy_after)}")

        if len(open_sell_after) == 1:
            log.info("[SELL FILL] Only 1 SELL order remaining, cancelling %s last BUY orders to free USDT for new SELL grid", config.REBALANCE_PREP_CANCEL_BUY_COUNT)
            canceled_count = await bot.cancel_last_n_buy_orders(config.REBALANCE_PREP_CANCEL_BUY_COUNT)

            if canceled_count > 0:
                bot._cancelled_buy_for_rebalance_prep = True
                await bot.ex.invalidate_balance_cache(bot.quote_asset_name)
                await asyncio.sleep(1)
                quote_available_after = await bot.ex.available_balance(bot.quote_asset_name)
                log.info(
                    f"[SELL FILL] After cancelling {canceled_count} BUY orders: available balance = {quote_available_after:.2f} {bot.quote_asset_name}"
                )

        if bot.profit_bank > 0:
            await bot.check_pyramiding()

        log.info("[SELL FILL] Step 3: Checking rebalancing")
        await bot.check_rebalancing(price)

        await asyncio.to_thread(bot.save_state)
        log.info(f"🟥 SELL fill processing completed for order {order.order_id}")

    except Exception as e:
        log.error(f"Error handling SELL fill: {e}", exc_info=True)
