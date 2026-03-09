"""
Ребалансировка: проверка «все SELL закрыты», рыночная покупка, перестроение BUY и создание SELL сетки.
Логика вынесена из TradingBot (см. SELL_REBALANCING.md, ARCHITECTURE_AND_SCENARIOS.md).
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from grid_protection import create_buy_orders_at_bottom

log = logging.getLogger("rebalance")


async def _rebalancing_apply_after_market_buy(bot: "TradingBot", market_result: dict, current_price: Decimal) -> bool:
    """После успешного market buy: обновить позицию, перестроить BUY сетку, создать SELL и при необходимости BUY внизу."""
    if not market_result or not market_result.get("orderId"):
        return False
    log.info(f"🟩 Market buy successful: orderId={market_result.get('orderId')}")
    await asyncio.sleep(3)
    try:
        order_info = await bot.ex.get_order(bot.symbol, market_result.get("orderId"))
        if order_info:
            executed_price = Decimal(str(order_info.get("price", current_price)))
            executed_qty = Decimal(str(order_info.get("executedQty", "0")))
            if executed_qty > 0:
                bot.position_manager.add_position(executed_price, executed_qty)
                log.info(f"Added position from market buy: {executed_qty} {bot.base_asset_name} at {executed_price}")
    except Exception as e:
        log.warning(f"Failed to get order info after market buy: {type(e).__name__}")

    await bot.ex.invalidate_balance_cache(bot.base_asset_name)
    await bot.ex.invalidate_balance_cache(bot.quote_asset_name)
    await asyncio.sleep(0.5)
    bot.base_asset = await bot.ex.balance(bot.base_asset_name)
    bot.current_deposit = await bot.ex.balance(bot.quote_asset_name)

    try:
        new_current_price = await bot.get_current_price()
        log.info(f"[REBALANCING] Got current price: {new_current_price:.8f}")
    except Exception as e:
        log.error(f"[REBALANCING] Failed to get current price: {type(e).__name__}")
        new_current_price = current_price

    log.info(f"[REBALANCING] Rebuilding BUY grid from new price: {new_current_price:.8f}")
    try:
        await bot.rebuild_buy_grid_from_price(new_current_price)
        log.info("🟩 [REBALANCING] ✅ BUY grid rebuilt successfully")
    except Exception as e:
        log.error(f"[REBALANCING] Failed to rebuild BUY grid: {type(e).__name__}", exc_info=True)
        log.warning("[REBALANCING] Continuing with SELL grid creation despite BUY grid rebuild issues")

    try:
        sell_created_count = await bot.create_sell_grid_only(new_current_price)
        log.info(f"🟥 [REBALANCING] ✅ SELL grid created: {sell_created_count} orders")
    except Exception as e:
        log.error(f"[REBALANCING] Failed to create SELL grid: {type(e).__name__}", exc_info=True)
        sell_created_count = 0

    if sell_created_count and sell_created_count >= 3:
        log.info(f"[REBALANCING] Creating BUY orders at bottom (sell_created_count={sell_created_count})")
        buy_created_count = await create_buy_orders_at_bottom(bot, new_current_price)
        log.info(f"🟩 [REBALANCING] Created {buy_created_count} BUY orders at bottom")
    else:
        log.info(f"[REBALANCING] Skipping BUY at bottom: sell_created_count={sell_created_count} (need >=3)")

    bot._cancelled_buy_for_rebalance_prep = False
    await asyncio.to_thread(bot.save_state)
    log.info("[REBALANCING] ✅ Rebalancing completed successfully")
    return True


async def check_rebalancing(bot: "TradingBot", current_price: Decimal) -> None:
    """Проверка на ребаланс (все SELL закрыты): рыночная покупка, перестроение BUY, создание SELL."""
    from trading_bot import BotState

    try:
        if bot.state == BotState.STOPPED:
            return
        open_sell_orders = [o for o in bot.orders if o.side == "SELL" and o.status == "open"]
        open_buy_orders = [o for o in bot.orders if o.side == "BUY" and o.status == "open"]

        log.info(f"[REBALANCING_CHECK] Memory: open SELL={len(open_sell_orders)}, open BUY={len(open_buy_orders)}, state={bot.state}")

        try:
            exchange_orders = await bot.ex.open_orders(bot.symbol)
            exchange_sell_orders = [o for o in exchange_orders if o.get("side") == "SELL"]
            log.info(f"[REBALANCING_CHECK] Exchange: open SELL={len(exchange_sell_orders)}")
        except Exception as e:
            log.error(f"[REBALANCING_CHECK] Failed to get exchange orders: {e}", exc_info=True)
            exchange_sell_orders = []

        if len(open_sell_orders) == 0 and len(exchange_sell_orders) == 0 and bot.state == BotState.TRADING:
            log.info("[REBALANCING_CHECK] ✅ Condition met: All SELL orders executed, starting rebalancing")
            log.info(
                f"Rebalancing: All SELL orders executed (checked both memory and exchange). Rebuilding BUY grid from current price: {current_price:.8f} ({len(open_buy_orders)} old BUY orders will be cancelled)"
            )

            await bot.ex.invalidate_balance_cache(bot.quote_asset_name)
            await asyncio.sleep(0.5)

            market_buy_amount_usdt = (bot.buy_order_value * Decimal("5")) + Decimal("2")
            quote_available = await bot.ex.available_balance(bot.quote_asset_name)
            quote_balance = await bot.ex.balance(bot.quote_asset_name)

            log.info(f"[REBALANCING] Balance check: available={quote_available}, total={quote_balance}, required={market_buy_amount_usdt}")

            if quote_available >= market_buy_amount_usdt:
                try:
                    log.info(
                        f"All SELL orders executed. Performing market buy for new SELL orders: {market_buy_amount_usdt} {bot.quote_asset_name} (5 × {bot.buy_order_value} + 2 USDT reserve)"
                    )

                    market_result = await bot.ex.place_market(bot.symbol, "BUY", qty=Decimal("0"), quote_order_qty=market_buy_amount_usdt)

                    if market_result and market_result.get("orderId"):
                        await _rebalancing_apply_after_market_buy(bot, market_result, current_price)
                    else:
                        log.warning(f"[REBALANCING] Market buy failed or no orderId in result: {market_result}")
                except Exception as e:
                    error_msg = str(e)
                    log.error(f"[REBALANCING] Exception during market buy: {error_msg}", exc_info=True)
                    if "Permission denied" in error_msg or "Spot Trading permission" in error_msg:
                        raise
                    elif "balance not enough" in error_msg.lower() or "insufficient" in error_msg.lower():
                        log.warning(
                            f"[REBALANCING] Insufficient balance for market buy: available={quote_available:.2f}, required={market_buy_amount_usdt:.2f}"
                        )
                        if quote_available > Decimal("1"):
                            adjusted_amount = quote_available - Decimal("1")
                            log.info(
                                f"[REBALANCING] Retrying with adjusted amount: {adjusted_amount:.2f} {bot.quote_asset_name} (available - 1 USDT reserve)"
                            )
                            try:
                                market_result = await bot.ex.place_market(bot.symbol, "BUY", qty=Decimal("0"), quote_order_qty=adjusted_amount)
                                if market_result and market_result.get("orderId"):
                                    await _rebalancing_apply_after_market_buy(bot, market_result, current_price)
                                else:
                                    log.warning(f"[REBALANCING] Market buy with adjusted amount failed: {market_result}")
                            except Exception as e2:
                                log.error(f"[REBALANCING] Failed to retry with adjusted amount: {e2}", exc_info=True)
                                log.warning(
                                    f"[REBALANCING] Cannot create new grid - insufficient balance. Available: {quote_available:.2f}, Required: {market_buy_amount_usdt:.2f}"
                                )
                        else:
                            log.warning(
                                f"[REBALANCING] Cannot create new grid - insufficient balance. Available: {quote_available:.2f}, Required: {market_buy_amount_usdt:.2f}"
                            )
                    else:
                        log.warning(f"[REBALANCING] Failed to perform market buy for rebalancing: {e}")
            else:
                log.warning(
                    f"[REBALANCING] Insufficient available balance for full market buy: available={quote_available:.2f}, total={quote_balance:.2f}, required={market_buy_amount_usdt:.2f}"
                )

                if quote_available > Decimal("1"):
                    adjusted_amount = quote_available - Decimal("1")
                    log.info(
                        f"[REBALANCING] Attempting market buy with available balance: {adjusted_amount:.2f} {bot.quote_asset_name} (available - 1 USDT reserve)"
                    )

                    try:
                        market_result = await bot.ex.place_market(bot.symbol, "BUY", qty=Decimal("0"), quote_order_qty=adjusted_amount)

                        if market_result and market_result.get("orderId"):
                            await _rebalancing_apply_after_market_buy(bot, market_result, current_price)
                        else:
                            log.warning(f"[REBALANCING] Market buy with available balance failed: {market_result}")
                    except Exception as e2:
                        error_msg = str(e2)
                        log.error(f"[REBALANCING] Failed to perform market buy with available balance: {error_msg}", exc_info=True)
                        if "balance not enough" in error_msg.lower() or "insufficient" in error_msg.lower():
                            log.warning(
                                f"[REBALANCING] Cannot create new grid - insufficient balance even for available amount. Available: {quote_available:.2f}"
                            )
                        else:
                            log.warning(f"[REBALANCING] Market buy failed with error: {error_msg}")
                else:
                    log.warning(
                        f"[REBALANCING] Cannot create new grid - insufficient balance. Available: {quote_available:.2f} (need > 1 USDT), Required: {market_buy_amount_usdt:.2f}"
                    )
        else:
            log.debug(
                f"[REBALANCING_CHECK] Condition not met: open_sell_memory={len(open_sell_orders)}, open_sell_exchange={len(exchange_sell_orders)}, state={bot.state}"
            )

    except Exception as e:
        log.error(f"[REBALANCING] Error checking rebalancing: {e}", exc_info=True)


async def check_rebalancing_after_all_buy_filled(bot: "TradingBot", current_price: Decimal) -> None:
    """Проверка ребалансировки после исполнения всех BUY: создание SELL сетки от VWAP."""
    from trading_bot import BotState

    try:
        exchange_orders = await bot.ex.open_orders(bot.symbol)
        exchange_buy_orders = [o for o in exchange_orders if o.get("side") == "BUY"]

        open_buy_orders = [o for o in bot.orders if o.side == "BUY" and o.status == "open"]

        if len(open_buy_orders) == 0 and len(exchange_buy_orders) == 0 and bot.state == BotState.TRADING:
            vwap = await bot.calculate_vwap()

            if vwap > 0:
                log.info(f"[REBALANCING_AFTER_BUY] All BUY orders executed. VWAP: {vwap:.8f}. Creating SELL grid from VWAP")

                result = await bot.create_critical_sell_grid()

                if result["created_count"] > 0:
                    log.info(f"🟥 [REBALANCING_AFTER_BUY] ✅ Successfully created {result['created_count']} SELL orders from VWAP {vwap:.8f}")
                else:
                    log.warning(f"[REBALANCING_AFTER_BUY] ⚠️ Failed to create SELL orders from VWAP. Created: {result['created_count']}")
            else:
                log.warning("[REBALANCING_AFTER_BUY] VWAP is 0, cannot create SELL grid from average price")

    except Exception as e:
        log.error(f"[REBALANCING_AFTER_BUY] Error checking rebalancing after all BUY filled: {e}", exc_info=True)
