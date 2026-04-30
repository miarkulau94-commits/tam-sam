"""
Telegram бот для управления AutoScaleX Pro 2.2
С реферальной системой и админкой
"""

import asyncio
import logging
import secrets
import threading
import time
from decimal import Decimal
from typing import Dict, Optional

import config
from exchange import BingXSpot
from error_handling import is_telegram_critical
from persistence import StatePersistence
from referral_system import ReferralSystem
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from trading_bot import BotState, TradingBot

log = logging.getLogger("telegram_bot")


def _create_trading_bot(user_id: int, api_key: str, secret: str, telegram_notifier, symbol: str):
    """Создать экземпляр TradingBot в отдельном потоке (load_state блокирует event loop)."""
    return TradingBot(user_id, api_key, secret, telegram_notifier, symbol)


def _fetch_open_orders_on_exchange(api_key: str, secret: str, symbol: str):
    """
    Синхронно запросить открытые ордера на бирже (для авто-восстановления).
    Возвращает список ордеров или [] при ошибке. Вызывать через asyncio.to_thread.
    """
    try:
        ex = BingXSpot(api_key, secret, None)
        orders = ex.open_orders(symbol)
        return orders if isinstance(orders, list) else []
    except Exception as e:
        log.warning("[AUTO-RESTORE] Exchange open_orders failed: %s", e)
        return []


def _process_uid_sync(persistence, referral_system, user_id: int, uid: str, username: str, is_admin: bool, expecting_uid: bool):
    """
    Вся работа с persistence и referral_system при вводе UID — в одном потоке.
    Возвращает dict с полем "action" и опциональными полями. Вызывать только через asyncio.to_thread.
    """
    import threading
    log.info("[UID] _process_uid_sync START (thread=%s) user_id=%s uid_len=%s expecting_uid=%s is_admin=%s", threading.current_thread().name, user_id, len(uid) if uid else 0, expecting_uid, is_admin)
    try:
        if not expecting_uid:
            state = persistence.load_state(user_id)
            has_uid = bool(state and state.get("uid"))
            log.info("[UID] _process_uid_sync DONE action=check has_uid=%s", has_uid)
            return {"action": "check", "has_uid": has_uid}

        if not uid:
            log.info("[UID] _process_uid_sync DONE action=empty_uid")
            return {"action": "empty_uid"}

        is_referral = referral_system.is_referral(uid)
        log.info("[UID] _process_uid_sync is_referral=%s", is_referral)
        if is_admin or is_referral:
            state = persistence.load_state(user_id) or {}
            state["uid"] = uid
            state["username"] = username or state.get("username", "")
            persistence.save_state(user_id, state)
            if not is_admin:
                info = referral_system.get_referral(uid)
                if info:
                    info["user_id"] = user_id
                    info["username"] = username
                    referral_system.save_referrals()
            log.info("[UID] _process_uid_sync DONE action=approved")
            return {"action": "approved"}

        added = referral_system.add_pending_referral(uid, user_id, username or None)
        if added:
            log.info("[UID] _process_uid_sync DONE action=pending_added")
            return {"action": "pending_added"}
        is_ref = referral_system.is_referral(uid)
        action = "already_approved" if is_ref else "pending_exists"
        log.info("[UID] _process_uid_sync DONE action=%s", action)
        return {"action": action}
    except Exception as e:
        log.exception("[UID] _process_uid_sync EXCEPTION: %s", e)
        raise

# Троттлинг критических уведомлений (не спамить Telegram)
_ERROR_NOTIFY_COOLDOWN = config.ERROR_NOTIFY_COOLDOWN
_last_error_notify: Dict[int, float] = {}
_error_notify_lock = threading.Lock()


async def _safe_edit_message(obj, text: str, **kwargs) -> None:
    """Вызов edit_message_text с игнорированием 'message is not modified'."""
    try:
        await obj.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


def _is_error_notification(msg: str) -> bool:
    """Проверить, является ли сообщение ошибкой (для троттлинга). Используется в тестах."""
    if msg is None:
        return False
    s = msg.strip()
    return (
        s.startswith("🚨")
        or s.startswith("⚠️")
        or "Критическая ошибка" in msg
        or "Circuit breaker" in msg
        or "Ошибка API ключа" in msg
        or "Превышено время ожидания" in msg
    )


def _is_success_or_info(msg: str) -> bool:
    """Успешные/информационные сообщения — отправляем всегда."""
    s = (msg or "").strip()
    return s.startswith("✅") or "Бот восстановлен" in s or "Бот инициализирован" in s or "успешно выставлены" in s


def _should_send_error_to_user(msg: str) -> bool:
    """Слать только критичные ошибки, не спамить balance not enough и т.п."""
    return is_telegram_critical(msg)


async def _throttled_send(app, chat_id: int, msg: str) -> None:
    """Отправить сообщение с троттлингом критических уведомлений."""
    if _is_error_notification(msg):
        with _error_notify_lock:
            now = time.time()
            last = _last_error_notify.get(chat_id, 0)
            if now - last < _ERROR_NOTIFY_COOLDOWN:
                log.debug("Telegram: критическое уведомление пропущено (троттлинг)")
                return
            _last_error_notify[chat_id] = now
    try:
        await app.bot.send_message(chat_id, msg)
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


def _make_telegram_notifier(app, user_id: int):
    """Создать notifier: успешные — всегда, ошибки — только критичные, с троттлингом."""

    async def _send(msg: str):
        if not msg:
            return
        if _is_success_or_info(msg):
            try:
                await app.bot.send_message(user_id, msg)
            except Exception as e:
                log.warning(f"Telegram send failed: {e}")
            return
        if not _should_send_error_to_user(msg):
            log.debug("Telegram: пропуск не-критичной ошибки")
            return
        await _throttled_send(app, user_id, msg)

    def notifier(msg: str):
        return asyncio.create_task(_send(msg))

    return notifier


class TelegramBotManager:
    """Менеджер Telegram бота с реферальной системой"""

    def __init__(self):
        self.app = None
        self.referral_system = ReferralSystem(config.REFERRALS_FILE)
        self.user_bots: Dict[int, TradingBot] = {}  # {user_id: TradingBot} - для быстрого доступа по user_id
        self.uid_bots: Dict[str, int] = {}  # {uid: user_id} - маппинг UID -> user_id для поиска бота по UID
        self.user_states: Dict[int, Dict] = {}  # Состояния пользователей (ожидание UID, настройки и т.д.)
        self.persistence = StatePersistence()
        # Короткие id для кнопок Одобрить/Отклонить (Telegram callback_data до 64 байт)
        self._pending_callback_map: Dict[str, str] = {}  # short_id -> uid

    def _get_user_uid(self, user_id: int) -> Optional[str]:
        """Получить UID пользователя"""
        state = self.persistence.load_state(user_id)
        if state and "uid" in state:
            return str(state["uid"])
        return str(user_id)  # Fallback на user_id

    def _get_bot_by_uid(self, uid: str) -> Optional[TradingBot]:
        """Получить бота по UID"""
        if uid in self.uid_bots:
            user_id = self.uid_bots[uid]
            if user_id in self.user_bots:
                return self.user_bots[user_id]
        return None

    def _get_or_create_bot_for_user(self, uid: str, user_id: int) -> Optional[TradingBot]:
        """Получить бота по UID, при отсутствии — по user_id с обновлением маппинга."""
        bot = self._get_bot_by_uid(uid)
        if bot:
            return bot
        if user_id in self.user_bots:
            bot = self.user_bots[user_id]
            self.uid_bots[uid] = user_id
            return bot
        return None

    async def restore_running_bots(self):
        """
        Авто-восстановление ботов после перезапуска (питание, интернет).
        Восстанавливаем только тех, у кого на бирже есть открытые ордера (проверка по API).
        После «Стоп» ордера отменены → на бирже 0 → не восстанавливаем.
        """
        if not self.app:
            log.warning("[AUTO-RESTORE] Skipped: app not set")
            return
        try:
            log.info("[AUTO-RESTORE] Starting (state_dir=%s)", getattr(self.persistence, "state_dir", "?"))
            user_ids = await asyncio.to_thread(self.persistence.list_user_ids_with_state)
            log.info("[AUTO-RESTORE] Found %s user(s) with state file", len(user_ids))
            restored = 0
            for user_id in user_ids:
                try:
                    state = await asyncio.to_thread(self.persistence.load_state, user_id)
                    if not state:
                        continue
                    if state.get("bot_state") == BotState.STOPPED.value:
                        log.info("[AUTO-RESTORE] user_id=%s skip: bot_state=STOPPED", user_id)
                        continue
                    uid = str(state.get("uid") or user_id)
                    symbol = state.get("symbol")
                    if not symbol or "-" not in symbol:
                        continue
                    api_key, secret = await asyncio.to_thread(
                        self._load_api_keys_for_user, user_id, uid, True
                    )
                    if not api_key or not secret:
                        log.info("[AUTO-RESTORE] user_id=%s uid=%s skip: no API keys", user_id, uid)
                        continue
                    # Проверяем биржу: восстанавливаем только если есть открытые ордера
                    exchange_orders = await asyncio.to_thread(
                        _fetch_open_orders_on_exchange, api_key, secret, symbol
                    )
                    if not exchange_orders or len(exchange_orders) == 0:
                        log.info("[AUTO-RESTORE] user_id=%s symbol=%s skip: no open orders on exchange", user_id, symbol)
                        continue
                    if user_id in self.user_bots:
                        log.info("[AUTO-RESTORE] user_id=%s already has bot, skip", user_id)
                        continue
                    notifier = _make_telegram_notifier(self.app, user_id)
                    bot = await asyncio.to_thread(
                        _create_trading_bot, user_id, api_key, secret, notifier, symbol
                    )
                    self.user_bots[user_id] = bot
                    self.uid_bots[uid] = user_id
                    asyncio.create_task(bot.main_loop())
                    restored += 1
                    log.info(
                        "[AUTO-RESTORE] Restored bot user_id=%s uid=%s symbol=%s (%s open orders on exchange)",
                        user_id, uid, symbol, len(exchange_orders),
                    )
                    # Уведомление в Telegram о восстановлении
                    try:
                        await bot.telegram_notifier(
                            f"🔄 Бот восстановлен после перезапуска\n\n"
                            f"Пара: {symbol}\n"
                            f"Открытых ордеров на бирже: {len(exchange_orders)}"
                        )
                    except Exception as notify_err:
                        log.warning("[AUTO-RESTORE] Notify failed for user_id=%s: %s", user_id, notify_err)
                except Exception as e:
                    log.warning("[AUTO-RESTORE] Failed for user_id=%s: %s", user_id, e, exc_info=True)
            log.info("[AUTO-RESTORE] Done. Restored %s bot(s)", restored)
        except Exception as e:
            log.error("[AUTO-RESTORE] Error: %s", e, exc_info=True)

    def _load_api_keys_for_user(self, user_id: int, uid: str, migrate: bool = True) -> tuple:
        """Загрузить API ключи: сначала из зашифрованного хранилища, иначе из state (fallback).
        При migrate=True при загрузке из state ключи сохраняются в зашифрованном виде."""
        api_keys = self.persistence.load_api_keys(uid)
        if api_keys:
            return api_keys
        state = self.persistence.load_state(user_id)
        api_key = state.get("api_key") if state else None
        secret = state.get("secret") if state else None
        if api_key and secret and migrate:
            try:
                self.persistence.save_api_keys(uid, api_key, secret)
                if state:
                    if "api_key" in state:
                        del state["api_key"]
                    if "secret" in state:
                        del state["secret"]
                    self.persistence.save_state(user_id, state)
                log.info(f"Migrated API keys to encrypted storage for UID {uid}")
            except (OSError, RuntimeError, KeyError, ValueError) as e:
                log.warning(f"Could not migrate API keys: {e}")
        return (api_key, secret)

    def _get_back_keyboard(self, callback_data: str = "back_to_menu") -> InlineKeyboardMarkup:
        """Клавиатура с кнопкой «Назад»."""
        return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=callback_data)]])

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /start - Проверка реферала и запуск"""
        user_id = update.effective_user.id
        username = update.effective_user.username
        log.info("[TG] cmd_start ENTER user_id=%s", user_id)

        # Проверяем, является ли пользователь админом
        is_admin = user_id == config.TG_ADMIN_ID

        # Загружаем и обновляем состояние (блокирующие операции — в потоке)
        def _load_and_maybe_save():
            log.info("[TG] cmd_start _load_and_maybe_save START (thread) user_id=%s", user_id)
            state = self.persistence.load_state(user_id)
            saved_uid = state.get("uid") if state else None
            saved_username = state.get("username") if state else None
            if state and username and (saved_username != username or not saved_username):
                state["username"] = username
                self.persistence.save_state(user_id, state)
                saved_username = username
            elif username and not state:
                state = {"username": username}
                self.persistence.save_state(user_id, state)
            log.info("[TG] cmd_start _load_and_maybe_save DONE user_id=%s saved_uid=%s", user_id, bool(saved_uid))
            return state, saved_uid, saved_username

        log.info("[TG] cmd_start calling to_thread(_load_and_maybe_save)...")
        state, saved_uid, saved_username = await asyncio.to_thread(_load_and_maybe_save)
        log.info("[TG] cmd_start to_thread returned saved_uid=%s", bool(saved_uid))

        # Если UID уже сохранен — проверяем, что реферал всё ещё в списке (админ мог удалить)
        if saved_uid and not is_admin:
            if not await asyncio.to_thread(self.referral_system.is_referral, saved_uid):
                # Реферал удалён — сбрасываем UID в state и идём по сценарию «нет доступа»
                if state:
                    state.pop("uid", None)
                    state.pop("username", None)
                    await asyncio.to_thread(self.persistence.save_state, user_id, state)
                log.info("[TG] cmd_start user_id=%s uid=%s was removed from referrals -> clearing state", user_id, saved_uid)
                saved_uid = None

        # Если UID сохранен и актуален (или админ), сразу открываем меню
        if saved_uid:
            await self.show_main_menu(update, context)
            return

        # Если UID не сохранен, проверяем что делать дальше
        # Очищаем состояние ожидания UID если было (на случай если пользователь использовал /start повторно)
        if user_id in self.user_states:
            self.user_states[user_id].pop("waiting_for_uid", None)
            # Если состояние пустое, удаляем его
            if not self.user_states[user_id]:
                del self.user_states[user_id]

        if is_admin:
            log.info("[TG] cmd_start user_id=%s is_admin -> show_main_menu", user_id)
            # Админ может использовать бот без UID - показываем меню сразу
            await self.show_main_menu(update, context)
        else:
            log.info("[TG] cmd_start user_id=%s set waiting_for_uid=True, reply prompt", user_id)
            # Для обычных пользователей запрашиваем UID только если он не сохранен
            self.user_states[user_id] = {"waiting_for_uid": True}
            await update.message.reply_text("👋 Добро пожаловать в AutoScaleX!\n\nДля начала работы введите ваш UID профиля:")
        log.info("[TG] cmd_start DONE user_id=%s", user_id)

    async def handle_uid_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка ввода UID. Вся работа с диском/рефералами — один вызов в потоке, цикл не блокируется."""
        user_id = update.effective_user.id
        uid = (update.message.text or "").strip()
        username_saved = (update.effective_user.username or "").strip()
        is_admin = user_id == config.TG_ADMIN_ID
        expecting_uid = user_id in self.user_states and bool(self.user_states[user_id].get("waiting_for_uid"))
        log.info("[UID] handle_uid_input ENTER user_id=%s uid_len=%s expecting_uid=%s is_admin=%s", user_id, len(uid) if uid else 0, expecting_uid, is_admin)

        try:
            log.info("[UID] handle_uid_input calling to_thread(_process_uid_sync)...")
            # Один вызов в поток: все load_state/save_state и referral_system только там
            result = await asyncio.to_thread(
                _process_uid_sync,
                self.persistence,
                self.referral_system,
                user_id,
                uid,
                username_saved,
                is_admin,
                expecting_uid,
            )
            log.info("[UID] handle_uid_input to_thread returned action=%s", result.get("action"))
        except Exception as e:
            log.exception("[UID] handle_uid_input error (user_id=%s): %s", user_id, e)
            try:
                await update.message.reply_text(
                    "Произошла ошибка при обработке UID. Попробуйте /start и введите UID снова."
                )
            except Exception:
                pass
            return

        action = result.get("action", "")

        if action == "empty_uid":
            await update.message.reply_text("Введите непустой UID профиля (скопируйте с биржи рефералов).")
            return
        if action == "check":
            if result.get("has_uid"):
                await update.message.reply_text("✅ UID уже сохранен. Используйте /start для входа в меню.")
            return
        if action == "approved":
            if user_id in self.user_states:
                del self.user_states[user_id]
            if is_admin:
                await update.message.reply_text("✅ UID сохранен! Доступ к торговле открыт.")
            else:
                await update.message.reply_text("✅ Реферал подтвержден! Доступ открыт.")
            await self.show_main_menu(update, context)
            return
        if action == "pending_added":
            log.info(f"New referral pending approval: {uid} (user_id: {user_id})")
            short_id = secrets.token_hex(4)
            self._pending_callback_map[short_id] = uid
            try:
                admin_message = (
                    f"⏳ **Новый реферал — требуется одобрение**\n\n"
                    f"UID: `{uid}`\n"
                    f"Username: @{username_saved or 'не указан'}\n"
                    f"User ID: `{user_id}`\n"
                    f"Время: {update.message.date.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Одобрить", callback_data=f"admin_approve_{short_id}"),
                        InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_reject_{short_id}"),
                    ],
                ])
                await context.bot.send_message(
                    chat_id=config.TG_ADMIN_ID, text=admin_message,
                    parse_mode="Markdown", reply_markup=keyboard
                )
            except Exception as e:
                log.warning(f"Failed to send referral approval request to admin: {e}")
            await update.message.reply_text(
                "⏳ Ваш запрос отправлен на одобрение администратору.\n\n"
                "Ожидайте подтверждения. После одобрения используйте /start для входа."
            )
            if user_id in self.user_states:
                del self.user_states[user_id]
            return
        if action == "already_approved":
            await update.message.reply_text("✅ Ваш UID уже одобрен. Используйте /start для входа в меню.")
        else:
            await update.message.reply_text(
                "⏳ Ваш запрос уже отправлен на одобрение. Ожидайте подтверждения администратора."
            )

    async def show_main_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать главное меню"""
        # Используем единую функцию для создания клавиатуры
        reply_markup = self._get_main_menu_keyboard()

        await update.message.reply_text("🎯 Главное меню AutoScaleX", reply_markup=reply_markup)

    async def callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик callback кнопок"""
        query = update.callback_query
        user_id = query.from_user.id
        data = (query.data or "")[:50]
        log.info("[TG] callback_handler ENTER user_id=%s data=%s", user_id, data)
        # Пытаемся ответить на callback query, но игнорируем ошибки для устаревших query
        try:
            await query.answer()
        except BadRequest as e:
            # Игнорируем ошибки для устаревших или недействительных callback query
            # (например, если бот был перезапущен и query уже истек)
            error_msg = str(e).lower()
            if "too old" in error_msg or "timeout" in error_msg or "query id is invalid" in error_msg:
                log.debug(f"Ignoring expired callback query: {e}")
                return  # Прерываем обработку для устаревших query
            else:
                log.warning(f"BadRequest when answering callback query: {e}")
                # Продолжаем обработку для других BadRequest ошибок
        except Exception as e:
            log.warning(f"Unexpected error answering callback query: {e}")
            # Продолжаем обработку для других ошибок

        # Проверка доступа (load_state — блокирующий, выполняем в потоке)
        log.info("[TG] callback_handler user_id=%s calling to_thread(_check_user_access)...", user_id)
        has_access = await asyncio.to_thread(self._check_user_access, user_id)
        log.info("[TG] callback_handler user_id=%s has_access=%s", user_id, has_access)
        if not has_access:
            await query.edit_message_text("❌ Доступ запрещен. Используйте /start для регистрации.")
            return

        if data == "start_bot":
            await self.handle_start_bot(query, user_id)
        elif data == "pause_bot":
            await self.handle_pause_bot(query, user_id)
        elif data == "stop_bot":
            await self.show_stop_confirmation(query)
        elif data == "confirm_stop_yes":
            await self.handle_stop_bot(query, user_id)
        elif data == "confirm_stop_no":
            await _safe_edit_message(
                query,
                "🎯 Главное меню AutoScaleX",
                reply_markup=self._get_main_menu_keyboard(),
            )
        elif data == "balance":
            await self.handle_balance(query, user_id)
        elif data == "orders":
            await self.handle_orders(query, user_id)
        elif data == "settings":
            await self.show_settings_menu(query, user_id)
        elif data == "symbol_custom":
            self.user_states[user_id] = {"waiting_for_symbol": True}
            await query.edit_message_text("Введите торговую пару в формате BTC-USDT:")
        elif data.startswith("grid_step_"):
            # Преобразуем "075" в 0.0075 (0.75%), "150" в 0.015 (1.5%)
            step_str = data.split("_")[2]
            step = Decimal(step_str) / Decimal("10000")
            await self.handle_set_grid_step(query, user_id, step)
        elif data.startswith("order_value_"):
            value = int(data.split("_")[2])
            await self.handle_set_order_value(query, user_id, value)
        elif data == "build_grid":
            await self.handle_build_grid(query, user_id)
        elif data == "select_symbol":
            await self.handle_select_symbol(query, user_id)
        elif data.startswith("symbol_"):
            symbol = data.replace("symbol_", "").replace("_", "-")
            await self.handle_set_symbol(query, user_id, symbol)
        elif data == "set_api_keys":
            await self.handle_set_api_keys(query, user_id)
        elif data == "use_saved_keys":
            await self.handle_use_saved_keys(query, user_id)
        elif data == "change_api_keys":
            await self.handle_change_api_keys(query, user_id)
        elif data == "rebalance_sell":
            await self.show_rebalance_confirmation(query)
        elif data == "confirm_rebalance_yes":
            await self.handle_rebalance_sell(query, user_id)
        elif data == "confirm_rebalance_no":
            await _safe_edit_message(
                query,
                "🎯 Главное меню AutoScaleX",
                reply_markup=self._get_main_menu_keyboard(),
            )
        elif data == "add_buy":
            await self.handle_add_buy(query, user_id)
        elif data == "back_to_menu":
            # Сбрасываем ожидание ввода (API ключи, символ), чтобы следующее сообщение не обрабатывалось как ввод
            if user_id in self.user_states:
                for key in ("waiting_for_api", "api_step", "api_key", "secret", "waiting_for_symbol"):
                    self.user_states[user_id].pop(key, None)
                if not self.user_states[user_id]:
                    del self.user_states[user_id]
            await query.edit_message_text("🎯 Главное меню AutoScaleX", reply_markup=self._get_main_menu_keyboard())
        elif data == "admin_list_referrals":
            await self.handle_admin_list_referrals(query)
        elif data == "admin_list_pending":
            await self.handle_admin_list_pending(query)
        elif data == "admin_add_referral":
            await self.handle_admin_add_referral(query)
        elif data == "admin_remove_referral":
            await self.handle_admin_remove_referral(query)
        elif data.startswith("admin_approve_"):
            key = data[len("admin_approve_"):].strip()
            uid = self._pending_callback_map.pop(key, None) or key  # short_id -> uid или legacy uid
            await self.handle_admin_approve_referral(query, uid, context)
        elif data.startswith("admin_reject_"):
            key = data[len("admin_reject_"):].strip()
            uid = self._pending_callback_map.pop(key, None) or key
            await self.handle_admin_reject_referral(query, uid, context)
        elif data == "admin_back":
            # Возврат в админ-панель из любого админ-меню
            if query.from_user.id == config.TG_ADMIN_ID:
                await self.show_admin_menu(query)
            else:
                await query.edit_message_text("❌ Доступ запрещен")

    def _check_user_access(self, user_id: int) -> bool:
        """Проверить доступ пользователя. Если реферал удалён из списка — UID сбрасывается, доступ отзывается."""
        if user_id == config.TG_ADMIN_ID:
            return True

        state = self.persistence.load_state(user_id)
        if not state:
            return False

        uid = state.get("uid")
        if not uid or uid == "":
            return False

        # Реферал мог быть удалён админом — проверяем актуальный список
        if not self.referral_system.is_referral(uid):
            state.pop("uid", None)
            state.pop("username", None)
            self.persistence.save_state(user_id, state)
            log.info("[TG] _check_user_access user_id=%s uid=%s removed from referrals -> access revoked", user_id, uid)
            return False

        return True

    def _get_main_menu_keyboard(self):
        """Получить клавиатуру главного меню (критичные кнопки — по одной в ряд, меньше риск случайного нажатия)."""
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("▶️ Старт", callback_data="start_bot")],
                [InlineKeyboardButton("⏸️ Пауза", callback_data="pause_bot")],
                [InlineKeyboardButton("🛑 Стоп", callback_data="stop_bot")],
                [
                    InlineKeyboardButton("💰 Баланс", callback_data="balance"),
                    InlineKeyboardButton("📋 Ордера", callback_data="orders"),
                ],
                [
                    InlineKeyboardButton("🔄 Ребалансировать SELL", callback_data="rebalance_sell"),
                    InlineKeyboardButton("➕ Добавить Buy", callback_data="add_buy"),
                ],
                [InlineKeyboardButton("🔑 Ввести API ключи", callback_data="set_api_keys")],
            ]
        )

    def _get_confirm_stop_keyboard(self):
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Да", callback_data="confirm_stop_yes"),
                    InlineKeyboardButton("Нет", callback_data="confirm_stop_no"),
                ],
            ]
        )

    def _get_confirm_rebalance_keyboard(self):
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Да", callback_data="confirm_rebalance_yes"),
                    InlineKeyboardButton("Нет", callback_data="confirm_rebalance_no"),
                ],
            ]
        )

    async def show_stop_confirmation(self, query):
        """Подтверждение перед остановкой бота (защита от случайного нажатия)."""
        await _safe_edit_message(
            query,
            "⚠️ Вы точно уверены в прекращении работы?\n\n"
            "Будут отменены все ордера, бот остановлен.",
            reply_markup=self._get_confirm_stop_keyboard(),
        )

    async def show_rebalance_confirmation(self, query):
        """Подтверждение перед ребалансировкой SELL (защита от случайного нажатия)."""
        await _safe_edit_message(
            query,
            "⚠️ Вы подтверждаете ребалансировку?\n\n"
            "SELL-сетка будет перестроена от VWAP.",
            reply_markup=self._get_confirm_rebalance_keyboard(),
        )

    async def handle_start_bot(self, query, user_id: int):
        """Обработка запуска бота - проверка ключей, показ баланса и настроек"""
        # Получаем UID пользователя (в потоке, чтобы не блокировать event loop)
        uid = await asyncio.to_thread(self._get_user_uid, user_id)
        if not uid:
            uid = str(user_id)  # Fallback на user_id

        # Проверяем, есть ли уже запущенный бот по UID
        bot = self._get_or_create_bot_for_user(uid, user_id)

        if bot:
            # Проверяем, есть ли открытые ордера
            open_orders = [o for o in bot.orders if o.status == "open"]

            if bot.state == BotState.TRADING:
                keyboard = self._get_main_menu_keyboard()
                if open_orders:
                    await _safe_edit_message(
                        query,
                        "⚠️ Бот уже запущен и работает!\n\n"
                        f"Открыто ордеров: {len(open_orders)}\n\n"
                        "Используйте команды:\n"
                        "• ⏸️ Пауза - приостановить торговлю\n"
                        "• 🛑 Стоп - остановить и отменить все ордера",
                        reply_markup=keyboard,
                    )
                else:
                    await _safe_edit_message(query, "⚠️ Бот уже запущен и работает", reply_markup=keyboard)
                return
            elif bot.state == BotState.PAUSED:
                keyboard = self._get_back_keyboard()
                if open_orders:
                    bot.state = BotState.TRADING
                    await _safe_edit_message(query, "▶️ Торговля возобновлена", reply_markup=keyboard)
                else:
                    bot.state = BotState.TRADING
                    await _safe_edit_message(query, "▶️ Торговля возобновлена", reply_markup=keyboard)
                return

        # Загружаем API ключи через единый метод
        api_key, secret = await asyncio.to_thread(self._load_api_keys_for_user, user_id, uid)
        if not api_key or not secret:
            await _safe_edit_message(
                query,
                "❌ Не настроены API ключи.\n\nИспользуйте кнопку '🔑 Ввести API ключи' для настройки.",
                reply_markup=self._get_main_menu_keyboard(),
            )
            return

        # Получаем состояние для настройки символа (в потоке)
        state = await asyncio.to_thread(self.persistence.load_state, user_id)
        saved_symbol = (state.get("symbol") if state else None) or config.SYMBOL

        # Если бота нет в памяти (например после Стоп или перезапуска) — создаём для проверки ордеров на бирже
        if not bot:
            notifier = _make_telegram_notifier(self.app, user_id)
            bot = await asyncio.to_thread(
                _create_trading_bot, user_id, api_key, secret, notifier, saved_symbol
            )
            self.user_bots[user_id] = bot
            self.uid_bots[uid] = user_id

        # Проверяем, есть ли на бирже открытые ордера — если да, подхватываем сетку и запускаем (без перехода в настройки)
        await _safe_edit_message(query, "🔄 Проверка ордеров на бирже...")
        try:
            exchange_orders = await bot.ex.open_orders(bot.symbol)
            if exchange_orders and len(exchange_orders) > 0:
                await asyncio.to_thread(bot.load_state)
                await bot.sync_orders_from_exchange()
                bot.state = BotState.INITIALIZING
                asyncio.create_task(bot.main_loop())
                log.info(f"[START_BOT] Resumed with {len(exchange_orders)} existing orders for user {user_id}")
                open_buy = len([o for o in bot.orders if o.side == "BUY" and o.status == "open"])
                open_sell = len([o for o in bot.orders if o.side == "SELL" and o.status == "open"])
                await _safe_edit_message(
                    query,
                    f"✅ **Бот запущен с существующей сеткой!**\n\n"
                    f"🟢 BUY: {open_buy} ордеров\n"
                    f"🔴 SELL: {open_sell} ордеров\n\n"
                    f"Бот работает в автоматическом режиме.",
                    reply_markup=self._get_main_menu_keyboard(),
                )
                return
        except Exception as e:
            log.warning(f"[START_BOT] Could not resume from exchange orders: {e}")

        # На бирже нет ордеров — показываем баланс и настройки (пользователь может построить сетку)
        await _safe_edit_message(query, "🔄 Проверка API ключей...")

        def _check_keys_and_balance():
            ex = BingXSpot(api_key, secret)
            sym = saved_symbol
            try:
                ex.symbol_info(sym)
            except Exception as e:
                log.warning(f"Symbol {sym} not found, using default {config.SYMBOL}: {e}")
                sym = config.SYMBOL
                ex.symbol_info(sym)
            if sym and "-" in sym:
                base_asset, quote_asset = sym.split("-")[0], sym.split("-")[1]
            else:
                base_asset, quote_asset = "BTC", "USDT"
            q_bal = ex.balance(quote_asset)
            b_bal = ex.balance(base_asset)
            price = ex.price(sym)
            return sym, base_asset, quote_asset, q_bal, b_bal, price

        try:
            saved_symbol, base_asset, quote_asset, quote_balance, base_balance, current_price = await asyncio.to_thread(
                _check_keys_and_balance
            )
            base_in_quote = base_balance * current_price
            total_equity = quote_balance + base_in_quote

            balance_message = (
                f"✅ **API ключи проверены!**\n\n"
                f"💰 **Баланс**\n\n"
                f"{quote_asset}: `{quote_balance:.2f}`\n"
                f"{base_asset}: `{base_balance:.6f}` ({base_in_quote:.2f} {quote_asset})\n\n"
                f"💵 Итого: `{total_equity:.2f} {quote_asset}`\n"
                f"📊 Цена {base_asset}: `{current_price:.2f} {quote_asset}`\n"
                f"📌 Торговая пара: `{saved_symbol}`"
            )

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("⚙️ Настроить параметры", callback_data="settings"),
                        InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu"),
                    ]
                ]
            )

            await _safe_edit_message(query, balance_message, parse_mode="Markdown", reply_markup=keyboard)

        except Exception as e:
            log.error(f"Error checking API keys for user {user_id}: {e}", exc_info=True)
            error_msg = str(e)
            if "Permission denied" in error_msg or "Spot Trading permission" in error_msg:
                error_msg = "❌ Ошибка: API ключ не имеет разрешения на Spot Trading.\n\nВключите разрешение 'Spot Trading' для вашего API ключа в настройках BingX."
            else:
                error_msg = f"❌ Ошибка проверки ключей: {error_msg}\n\nПроверьте правильность API ключей."
            await _safe_edit_message(query, error_msg, reply_markup=self._get_back_keyboard())

    async def handle_pause_bot(self, query, user_id: int):
        """Обработка паузы"""
        keyboard = self._get_back_keyboard()
        # Получаем UID пользователя
        uid = await asyncio.to_thread(self._get_user_uid, user_id)
        if not uid:
            await query.edit_message_text("❌ UID не найден. Используйте /start для настройки.", reply_markup=keyboard)
            return

        bot = self._get_or_create_bot_for_user(uid, user_id)
        if not bot:
            await query.edit_message_text("❌ Бот не запущен", reply_markup=keyboard)
            return

        if bot.state == BotState.PAUSED:
            bot.state = BotState.TRADING
            await asyncio.to_thread(bot.save_state)
            await query.edit_message_text("▶️ Торговля возобновлена", reply_markup=keyboard)
        else:
            bot.state = BotState.PAUSED
            await asyncio.to_thread(bot.save_state)
            await query.edit_message_text("⏸️ Торговля приостановлена", reply_markup=keyboard)

    async def handle_stop_bot(self, query, user_id: int):
        """Обработка остановки"""
        keyboard_back = self._get_back_keyboard()

        # Получаем UID пользователя
        uid = await asyncio.to_thread(self._get_user_uid, user_id)
        if not uid:
            await query.edit_message_text("❌ UID не найден. Используйте /start для настройки.", reply_markup=keyboard_back)
            return

        bot = self._get_or_create_bot_for_user(uid, user_id)
        if not bot:
            await query.edit_message_text("❌ Бот не запущен", reply_markup=keyboard_back)
            return

        # Сначала ставим STOPPED, чтобы main_loop и обработка ордеров не выставляли новые ордера
        bot.state = BotState.STOPPED
        bot._clear_pending_hedge()

        # Отменяем все ордера на бирже
        try:
            await bot.ex.cancel_all(bot.symbol)
        except Exception as e:
            log.warning(f"Error canceling orders: {e}")

        # Уведомление пользователю о полной остановке (в чат)
        stop_msg = "🛑 Бот полностью остановлен. Все ордера отменены."
        if bot.telegram_notifier:
            try:
                await bot.telegram_notifier(stop_msg)
            except Exception as e:
                log.warning(f"Failed to send stop notification: {e}")

        # Очистка сделок пользователя: файл сделок, статистика, счётчики (при смене пары — только новые сделки)
        bot.total_executed_buys = 0
        bot.total_executed_sells = 0
        if getattr(bot, "statistics", None):
            await asyncio.to_thread(bot.statistics.clear_all)

        # Сброс Profit Bank и базы для прибыли: после Стоп при следующем запуске/новой монете не показываем старую прибыль
        try:
            price = await bot.get_current_price()
            total_equity = await bot.get_total_equity(price)
            bot.initial_equity = total_equity
            bot.profit_bank = Decimal("0")
        except Exception as e:
            log.warning(f"[STOP_BOT] Could not set initial_equity/profit_bank: {e}")

        # Сохраняем состояние (и в user_data по UID), чтобы при выборе новой монеты и построении сетки не подтягивались старые данные
        await asyncio.to_thread(bot.save_state)

        # Удаляем бота из списков
        if user_id in self.user_bots:
            del self.user_bots[user_id]
        if uid in self.uid_bots:
            del self.uid_bots[uid]

        keyboard = self._get_back_keyboard()
        await query.edit_message_text(stop_msg, reply_markup=keyboard)

    async def handle_balance(self, query, user_id: int):
        """Показать баланс"""
        keyboard_back = self._get_back_keyboard()

        # Получаем UID пользователя
        uid = await asyncio.to_thread(self._get_user_uid, user_id)
        if not uid:
            await query.edit_message_text("❌ UID не найден. Используйте /start для настройки.", reply_markup=keyboard_back)
            return

        bot = self._get_or_create_bot_for_user(uid, user_id)
        if not bot:
            await query.edit_message_text("❌ Бот не запущен", reply_markup=keyboard_back)
            return

        try:
            # Подтягиваем состояние с диска (profit_bank, initial_equity и т.д.), не перезаписывая bot.state,
            # чтобы открытие «Баланс» не останавливало работающий бот (файл мог содержать STOPPED из прошлой сессии)
            await asyncio.to_thread(bot.load_state, True)  # skip_bot_state=True

            if bot.profit_bank < 0:
                bot.profit_bank = Decimal("0")

            try:
                if getattr(bot, "state", None) != BotState.STOPPED:
                    await bot.sync_orders_from_exchange(max_get_order=config.SYNC_BALANCE_MAX_GET_ORDER)
            except Exception as sync_err:
                log.debug("Balance: sync_orders_from_exchange skipped: %s", sync_err)

            price = await bot.get_current_price()
            quote_balance = await bot.ex.balance(bot.quote_asset_name)
            base_balance = await bot.ex.balance(bot.base_asset_name)
            base_in_quote = base_balance * price
            total_equity = await bot.get_total_equity(price)

            # Если начальный капитал не задан — фиксируем текущий итог как базовый уровень и сохраняем
            if bot.initial_equity <= 0:
                bot.initial_equity = total_equity
                await asyncio.to_thread(bot.save_state)

            if bot.initial_equity > 0:
                profit = total_equity - bot.initial_equity
                roi = (profit / bot.initial_equity) * Decimal("100")
            else:
                profit = Decimal("0")
                roi = Decimal("0")

            avg_sell = bot.average_open_sell_price()
            avg_sell_line = (
                f"📉 Средняя цена открытых SELL: `{avg_sell:.2f} {bot.quote_asset_name}`\n"
                if avg_sell is not None
                else f"📉 Средняя цена открытых SELL: `—`\n"
            )

            # Profit Bank — накопленная только положительная прибыль; пирамидинг при >= buy_order_value (см. check_pyramiding)
            message = (
                f"💰 **Баланс**\n\n"
                f"{bot.quote_asset_name}: `{quote_balance:.2f}`\n"
                f"{bot.base_asset_name}: `{base_balance:.6f}` ({base_in_quote:.2f} {bot.quote_asset_name})\n\n"
                f"💵 Итого: `{total_equity:.2f} {bot.quote_asset_name}`\n"
                f"📈 Прибыль: `{profit:.2f} {bot.quote_asset_name}` ({roi:.2f}%)\n"
                f"📊 Цена {bot.base_asset_name}: `{price:.2f} {bot.quote_asset_name}`\n"
                f"{avg_sell_line}"
                f"💎 Profit Bank: `{bot.profit_bank:.2f} {bot.quote_asset_name}` (для пирамидинга)\n"
                f"📌 Пара: `{bot.symbol}`"
            )

            keyboard = self._get_back_keyboard()

            await _safe_edit_message(query, message, parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            err_msg = str(e)
            if "Circuit breaker" in err_msg or "circuit breaker" in err_msg.lower():
                log.warning(f"Balance check skipped (Circuit breaker): {e}")
            else:
                log.error(f"Error getting balance: {e}")
            keyboard_back = self._get_back_keyboard()
            await query.edit_message_text(f"❌ Ошибка: {e}", reply_markup=keyboard_back)

    async def handle_orders(self, query, user_id: int):
        """Показать ордера"""
        keyboard_back = self._get_back_keyboard()

        # Получаем UID пользователя
        uid = await asyncio.to_thread(self._get_user_uid, user_id)
        if not uid:
            await query.edit_message_text("❌ UID не найден. Используйте /start для настройки.", reply_markup=keyboard_back)
            return

        bot = self._get_or_create_bot_for_user(uid, user_id)
        if not bot:
            await query.edit_message_text("❌ Бот не запущен", reply_markup=keyboard_back)
            return

        try:
            open_orders = [o for o in bot.orders if o.status == "open"]
            buy_orders = [o for o in open_orders if o.side == "BUY"]
            sell_orders = [o for o in open_orders if o.side == "SELL"]
            # Только реально исполненные: из записей о сделках по текущей паре
            trades = getattr(bot.statistics, "trades", []) or []
            executed_buy = len([t for t in trades if t.get("type") == "BUY" and t.get("symbol") == bot.symbol])
            executed_sell = len([t for t in trades if t.get("type") == "SELL" and t.get("symbol") == bot.symbol])

            message = (
                f"📋 **Ордера**\n\n"
                f"🟢 BUY: `{len(buy_orders)}` ордеров\n"
                f"🔴 SELL: `{len(sell_orders)}` ордеров\n\n"
                f"✅ Исполнено BUY: `{executed_buy}`\n"
                f"✅ Исполнено SELL: `{executed_sell}`"
            )

            keyboard = self._get_back_keyboard()

            await query.edit_message_text(message, parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            err_msg = str(e)
            if "Circuit breaker" in err_msg or "circuit breaker" in err_msg.lower():
                log.warning(f"Orders display skipped (Circuit breaker): {e}")
            else:
                log.error(f"Error getting orders: {e}")
            keyboard_back = self._get_back_keyboard()
            await query.edit_message_text(f"❌ Ошибка: {e}", reply_markup=keyboard_back)

    async def show_settings_menu(self, query, user_id: int):
        """Показать меню настроек"""
        # Получаем сохраненные настройки из состояния (в потоке)
        state = await asyncio.to_thread(self.persistence.load_state, user_id)

        # Получаем UID пользователя
        uid = await asyncio.to_thread(self._get_user_uid, user_id)
        if not uid:
            uid = str(user_id)  # Fallback на user_id

        # Проверяем наличие API ключей через единый метод
        api_key, secret = await asyncio.to_thread(self._load_api_keys_for_user, user_id, uid)
        if not api_key or not secret:
            await query.edit_message_text(
                "❌ Не настроены API ключи.\n\nИспользуйте кнопку '🔑 Ввести API ключи' для настройки.", reply_markup=self._get_main_menu_keyboard()
            )
            return

        # Загружаем настройки
        saved_symbol = (state.get("symbol") if state else None) or config.SYMBOL
        saved_grid_step = (state.get("grid_step_pct") if state else None) or config.GRID_STEP_PCT
        saved_order_value = (state.get("buy_order_value") if state else None) or config.BUY_ORDER_VALUE

        # Преобразуем в Decimal если нужно
        if saved_grid_step and not isinstance(saved_grid_step, Decimal):
            saved_grid_step = Decimal(str(saved_grid_step))
        if saved_order_value and not isinstance(saved_order_value, Decimal):
            saved_order_value = Decimal(str(saved_order_value))

        # Конвертируем только если сохранено как процент (0.75 или 1.5), НЕ десятичное (0.0075, 0.015)
        # Значения 0.0075 и 0.015 уже в правильном формате — не делить на 100!
        if saved_grid_step and saved_grid_step >= Decimal("0.5"):
            saved_grid_step = saved_grid_step / Decimal("100")

        # Определяем quote asset (проверяем что saved_symbol не None)
        if saved_symbol and "-" in saved_symbol:
            quote_asset = saved_symbol.split("-")[1]
        else:
            quote_asset = "USDT"

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📌 Выбрать монету", callback_data="select_symbol"),
                ],
                [
                    InlineKeyboardButton("📏 Шаг сетки (0.75%)", callback_data="grid_step_075"),
                    InlineKeyboardButton("📏 Шаг сетки (1.5%)", callback_data="grid_step_150"),
                ],
                [
                    InlineKeyboardButton("💵 10 USDT", callback_data="order_value_10"),
                    InlineKeyboardButton("💵 20 USDT", callback_data="order_value_20"),
                    InlineKeyboardButton("💵 35 USDT", callback_data="order_value_35"),
                ],
                [
                    InlineKeyboardButton("💵 50 USDT", callback_data="order_value_50"),
                    InlineKeyboardButton("💵 75 USDT", callback_data="order_value_75"),
                    InlineKeyboardButton("💵 100 USDT", callback_data="order_value_100"),
                ],
                [
                    InlineKeyboardButton("💵 175 USDT", callback_data="order_value_175"),
                    InlineKeyboardButton("💵 250 USDT", callback_data="order_value_250"),
                    InlineKeyboardButton("💵 350 USDT", callback_data="order_value_350"),
                ],
                [
                    InlineKeyboardButton("🔨 Построить сетку", callback_data="build_grid"),
                ],
                [
                    InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu"),
                ],
            ]
        )

        message = (
            f"⚙️ **Настройки**\n\n"
            f"Торговая пара: `{saved_symbol}`\n"
            f"Шаг сетки: `{saved_grid_step * 100:.2f}%`\n"
            f"Размер ордера: `{saved_order_value} {quote_asset}`\n\n"
            f"Выберите параметры и нажмите 'Построить сетку' для создания сетки ордеров."
        )

        await query.edit_message_text(message, parse_mode="Markdown", reply_markup=keyboard)

    async def handle_set_grid_step(self, query, user_id: int, step: Decimal):
        """Установить шаг сетки"""
        # Сохраняем в состоянии пользователя (в потоке)
        state = await asyncio.to_thread(self.persistence.load_state, user_id) or {}
        state["grid_step_pct"] = step
        await asyncio.to_thread(self.persistence.save_state, user_id, state)

        # Обновляем в боте, если он уже создан
        if user_id in self.user_bots:
            bot = self.user_bots[user_id]
            bot.grid_step_pct = step
            await asyncio.to_thread(bot.save_state)

        await query.edit_message_text(
            f"✅ Шаг сетки установлен: {step * 100:.2f}%",
            reply_markup=self._get_back_keyboard(),
        )
        await asyncio.sleep(1)
        await self.show_settings_menu(query, user_id)

    async def handle_set_order_value(self, query, user_id: int, value: int):
        """Установить размер ордера"""
        # Сохраняем в состоянии пользователя (в потоке)
        state = await asyncio.to_thread(self.persistence.load_state, user_id) or {}
        state["buy_order_value"] = Decimal(str(value))
        await asyncio.to_thread(self.persistence.save_state, user_id, state)

        # Обновляем в боте, если он уже создан
        if user_id in self.user_bots:
            bot = self.user_bots[user_id]
            bot.buy_order_value = Decimal(str(value))
            await asyncio.to_thread(bot.save_state)

        await query.edit_message_text(
            f"✅ Размер ордера установлен: {value} USDT",
            reply_markup=self._get_back_keyboard(),
        )
        await asyncio.sleep(1)
        await self.show_settings_menu(query, user_id)

    async def handle_build_grid(self, query, user_id: int):
        """Построить сетку - создает бота, запускает его и строит сетку"""
        # Получаем UID пользователя
        uid = await asyncio.to_thread(self._get_user_uid, user_id)
        if not uid:
            uid = str(user_id)  # Fallback на user_id

        # Проверяем, есть ли уже запущенный бот с открытыми ордерами по UID
        bot = self._get_or_create_bot_for_user(uid, user_id)

        if bot:
            open_orders = [o for o in bot.orders if o.status == "open"]
            if bot.state == BotState.TRADING and open_orders:
                keyboard_back = self._get_back_keyboard()
                await query.edit_message_text(
                    "⚠️ Невозможно построить сетку!\n\n"
                    f"Бот уже запущен и работает ({len(open_orders)} открытых ордеров).\n\n"
                    "Сначала остановите бота командой 🛑 Стоп, затем можно построить новую сетку.",
                    reply_markup=keyboard_back,
                )
                return

        # Получаем UID пользователя
        uid = await asyncio.to_thread(self._get_user_uid, user_id)
        if not uid:
            await query.edit_message_text("❌ UID не найден. Используйте /start для настройки.", reply_markup=self._get_back_keyboard())
            return

        # Загружаем API ключи через единый метод
        api_key, secret = await asyncio.to_thread(self._load_api_keys_for_user, user_id, uid)
        if not api_key or not secret:
            await query.edit_message_text(
                "❌ Не настроены API ключи.\n\nИспользуйте кнопку '🔑 Ввести API ключи' для настройки.",
                reply_markup=self._get_main_menu_keyboard(),
            )
            return

        # Загружаем сохраненные настройки (в потоке)
        state = await asyncio.to_thread(self.persistence.load_state, user_id)
        saved_symbol = (state.get("symbol") if state else None) or config.SYMBOL
        saved_grid_step = (state.get("grid_step_pct") if state else None) or config.GRID_STEP_PCT
        saved_order_value = (state.get("buy_order_value") if state else None) or config.BUY_ORDER_VALUE

        # Преобразуем в Decimal если нужно
        if saved_grid_step and not isinstance(saved_grid_step, Decimal):
            saved_grid_step = Decimal(str(saved_grid_step))
        if saved_order_value and not isinstance(saved_order_value, Decimal):
            saved_order_value = Decimal(str(saved_order_value))

        # Конвертируем только если сохранено как процент (0.75 или 1.5), НЕ десятичное (0.0075, 0.015)
        if saved_grid_step and saved_grid_step >= Decimal("0.5"):
            saved_grid_step = saved_grid_step / Decimal("100")
        # Не применять ошибочный шаг 0.65% (в UI только 0.75% и 1.5%)
        if saved_grid_step in (Decimal("0.0065"), Decimal("0.65")):
            saved_grid_step = config.GRID_STEP_PCT

        await query.edit_message_text("🔄 Создание бота и построение сетки...")

        try:
            # Создаем или обновляем бота с текущими настройками
            if user_id in self.user_bots:
                # Если бот уже существует, останавливаем его
                bot = self.user_bots[user_id]
                bot.state = BotState.PAUSED
                try:
                    await bot.ex.cancel_all(bot.symbol)
                except Exception:
                    pass

            # Создаем бота в потоке: __init__ вызывает load_state() и блокирует event loop
            notifier = _make_telegram_notifier(self.app, user_id)
            bot = await asyncio.to_thread(
                _create_trading_bot, user_id, api_key, secret, notifier, saved_symbol
            )

            # Применяем сохраненные настройки (бот уже загрузил их в load_state при __init__)
            # Перезаписываем только если у нас есть явно загруженные значения — иначе доверяем load_state
            # ВАЖНО: не перезаписывать bot.grid_step_pct — load_state уже загрузил правильное значение.
            # Перезапись saved_grid_step могла портить 1.5% → 0.02% из-за рассинхрона источников данных.
            if saved_grid_step is not None and saved_grid_step >= Decimal("0.001"):
                bot.grid_step_pct = saved_grid_step
            if saved_order_value is not None and saved_order_value > 0:
                bot.buy_order_value = Decimal(str(saved_order_value))

            # Сохраняем бота в оба списка (user_id и UID)
            self.user_bots[user_id] = bot
            self.uid_bots[uid] = user_id
            # Обновляем маппинг UID -> user_id
            self.uid_bots[uid] = user_id

            # Сохраняем состояние бота (в потоке)
            await asyncio.to_thread(bot.save_state)

            # Проверяем, есть ли уже открытые ордера на бирже
            try:
                exchange_orders = await bot.ex.open_orders(bot.symbol)
                log.info(f"[BUILD_GRID] Found {len(exchange_orders) if exchange_orders else 0} existing orders on exchange for {bot.symbol}")

                if exchange_orders and len(exchange_orders) > 0:
                    # Есть существующие ордера - сначала загружаем параметры из state, потом синхронизируем ордера с биржи
                    log.info(f"[BUILD_GRID] Found {len(exchange_orders)} existing orders, resuming bot instead of creating new grid")
                    await asyncio.to_thread(bot.load_state)
                    await bot.sync_orders_from_exchange()

                    # Сбрасываем STOPPED из прошлой сессии (Стоп → смена пары → Построить сетку), иначе main_loop сразу выйдет
                    bot.state = BotState.INITIALIZING
                    # Запускаем основной цикл бота в фоне (main_loop проверит ордера и переведёт в TRADING)
                    log.info(f"🚀 Starting main_loop for user {user_id}, bot will resume with existing orders")
                    asyncio.create_task(bot.main_loop())
                    log.info(f"✅ main_loop task created for user {user_id}")

                    # Получаем количество ордеров после синхронизации
                    open_buy = len([o for o in bot.orders if o.side == "BUY" and o.status == "open"])
                    open_sell = len([o for o in bot.orders if o.side == "SELL" and o.status == "open"])

                    keyboard = self._get_back_keyboard()

                    await query.edit_message_text(
                        f"✅ **Бот восстановлен с существующими ордерами!**\n\n"
                        f"🟢 BUY: {open_buy} ордеров\n"
                        f"🔴 SELL: {open_sell} ордеров\n\n"
                        f"Бот работает в автоматическом режиме.",
                        reply_markup=keyboard,
                    )
                    return
            except Exception as e:
                log.warning(f"[BUILD_GRID] Error checking for existing orders: {e}, will create new grid")

            # Нет существующих ордеров - создаем новую сетку
            # Сбрасываем прибыль и профит-банк, чтобы при смене монеты не тянуть старые данные
            bot.profit_bank = Decimal("0")
            bot.initial_equity = Decimal("0")
            bot.total_executed_buys = 0
            bot.total_executed_sells = 0
            log.info("[BUILD_GRID] No existing orders found, creating new grid (profit_bank/initial_equity reset)")
            await bot.create_grid()

            # Получаем количество созданных ордеров
            created_buy = len([o for o in bot.orders if o.side == "BUY" and o.status == "open"])
            created_sell = len([o for o in bot.orders if o.side == "SELL" and o.status == "open"])

            # Запускаем основной цикл бота в фоне
            bot.state = BotState.TRADING
            await asyncio.to_thread(bot.save_state)  # чтобы при открытии «Баланс» load_state не подменял state на старый STOPPED
            log.info(f"🚀 Starting main_loop for user {user_id}, state set to TRADING")
            asyncio.create_task(bot.main_loop())
            log.info(f"✅ main_loop task created for user {user_id}")

            keyboard = self._get_back_keyboard()

            await query.edit_message_text(
                f"✅ **Сетка построена и бот запущен!**\n\n"
                f"🟢 BUY: {created_buy} ордеров\n"
                f"🔴 SELL: {created_sell} ордеров\n\n"
                f"Бот работает в автоматическом режиме.",
                reply_markup=keyboard,
            )
        except Exception as e:
            log.error(f"Error building grid: {e}", exc_info=True)
            keyboard_back = self._get_back_keyboard()
            error_msg = str(e)
            if "DivisionByZero" in str(type(e)) or "division by zero" in error_msg.lower():
                error_msg = "Ошибка: деление на ноль. Проверьте настройки размера ордера."
            elif "Permission denied" in error_msg or "Spot Trading permission" in error_msg:
                error_msg = "❌ Ошибка: API ключ не имеет разрешения на Spot Trading.\n\nВключите разрешение 'Spot Trading' для вашего API ключа в настройках BingX."
            await query.edit_message_text(f"❌ Ошибка построения сетки:\n{error_msg}", reply_markup=keyboard_back)

    async def handle_rebalance_sell(self, query, user_id: int):
        """Ребалансировать SELL сетку от VWAP"""
        log.info(f"[REBALANCE_SELL] User {user_id} requested SELL rebalancing")

        keyboard_back = self._get_back_keyboard()

        # Получаем UID пользователя
        uid = await asyncio.to_thread(self._get_user_uid, user_id)
        if not uid:
            log.warning(f"[REBALANCE_SELL] UID not found for user {user_id}")
            await query.edit_message_text("❌ UID не найден. Используйте /start для настройки.", reply_markup=keyboard_back)
            return

        log.info(f"[REBALANCE_SELL] User {user_id} has UID {uid}")

        bot = self._get_or_create_bot_for_user(uid, user_id)
        if not bot:
            log.error(f"[REBALANCE_SELL] Bot not found for user {user_id}, UID {uid}")
            await query.edit_message_text("❌ Бот не запущен", reply_markup=keyboard_back)
            return

        # Обновляем маппинг
        self.uid_bots[uid] = user_id
        if user_id not in self.user_bots:
            self.user_bots[user_id] = bot

        log.info(f"[REBALANCE_SELL] Starting rebalancing for user {user_id}")
        await query.edit_message_text("🔄 Ребалансировка SELL сетки от VWAP...")

        try:
            result = await bot.create_critical_sell_grid(vwap_source="manual_telegram")
            log.info(f"[REBALANCE_SELL] Rebalancing completed (manual VWAP / Telegram): {result}")

            keyboard = self._get_back_keyboard()

            if result["created_count"] == 0:
                error_msg = "❌ Не удалось создать SELL ордера.\n\n"
                error_msg += f"VWAP: {result['vwap']:.8f}\n"
                error_msg += "Возможные причины:\n"
                error_msg += "• Недостаточно базовой валюты\n"
                error_msg += "• Ордера не проходят валидацию (minQty/minNotional)\n"
                error_msg += "• Проблемы с API"
                await query.edit_message_text(error_msg, reply_markup=keyboard)
                return

            success_msg = "✅ SELL сетка перестроена от VWAP\n\n"
            success_msg += f"📊 Средняя цена покупки (VWAP): `{result['vwap']:.8f}`\n"
            success_msg += f"📈 Создано ордеров: `{result['created_count']}/{config.CRITICAL_SELL_DIVISIONS}`\n\n"

            if result["orders_info"]:
                success_msg += "📋 Созданные ордера:\n"
                for i, order_info in enumerate(result["orders_info"], 1):
                    success_msg += f"`{i}.` Цена: `{order_info['price']:.8f}` "
                    success_msg += f"(+{order_info['profit_pct']:.2f}%) "
                    success_msg += f"Кол-во: `{order_info['qty']:.8f}`\n"

            keyboard = self._get_back_keyboard()
            await query.edit_message_text(success_msg, parse_mode="Markdown", reply_markup=keyboard)

        except Exception as e:
            log.error(f"Error rebalancing sell grid: {e}", exc_info=True)
            keyboard = self._get_back_keyboard()
            error_msg = f"❌ Ошибка при ребалансировке:\n\n`{str(e)}`"
            await query.edit_message_text(error_msg, parse_mode="Markdown", reply_markup=keyboard)

    async def handle_add_buy(self, query, user_id: int):
        """Добавить BUY ордер(а) внизу сетки"""
        keyboard_back = self._get_back_keyboard()

        uid = await asyncio.to_thread(self._get_user_uid, user_id)
        if not uid:
            await query.edit_message_text("❌ UID не найден. Используйте /start для настройки.", reply_markup=keyboard_back)
            return

        bot = self._get_or_create_bot_for_user(uid, user_id)
        if not bot:
            await query.edit_message_text("❌ Бот не запущен. Сначала нажмите ▶️ Старт.", reply_markup=keyboard_back)
            return

        self.uid_bots[uid] = user_id
        if user_id not in self.user_bots:
            self.user_bots[user_id] = bot

        await query.edit_message_text("🔄 Добавление BUY ордера(ов) внизу сетки...")

        try:
            current_price = await bot.get_current_price()
            created = await bot.create_buy_orders_at_bottom(current_price)
            await asyncio.to_thread(bot.save_state)

            if created > 0:
                await query.edit_message_text(
                    f"✅ Добавлено BUY ордеров: {created}\n\nРазмер ордера: {bot.buy_order_value:.0f} {bot.quote_asset_name}",
                    reply_markup=keyboard_back,
                )
            else:
                msg = "⚠️ BUY ордера не созданы.\n\n"
                msg += "Возможные причины:\n"
                msg += "• Недостаточно баланса USDT\n"
                msg += "• Достигнут лимит BUY ордеров\n"
                msg += "• Нет свободного места внизу сетки"
                await query.edit_message_text(msg, reply_markup=keyboard_back)
        except Exception as e:
            log.error(f"Error adding BUY order: {e}", exc_info=True)
            await query.edit_message_text(
                f"❌ Ошибка: `{str(e)}`",
                parse_mode="Markdown",
                reply_markup=keyboard_back,
            )

    async def handle_select_symbol(self, query, user_id: int):
        """Показать меню выбора символа — запрашивает актуальные пары из API BingX"""
        # Получаем сохраненный символ (в потоке)
        state = await asyncio.to_thread(self.persistence.load_state, user_id)
        current_symbol = state.get("symbol", config.SYMBOL) if state else config.SYMBOL

        # Если бот уже создан, используем символ из бота
        if user_id in self.user_bots:
            current_symbol = self.user_bots[user_id].symbol

        # Популярные пары (fallback, если API недоступен)
        popular_symbols = [
            "BTC-USDT",
            "ETH-USDT",
            "BNB-USDT",
            "SOL-USDT",
            "XRP-USDT",
            "ADA-USDT",
            "DOGE-USDT",
            "DOT-USDT",
            "MATIC-USDT",
            "AVAX-USDT",
            "LINK-USDT",
            "UNI-USDT",
        ]

        # Загружаем актуальные пары из API BingX через единый метод
        api_symbols = []
        uid = await asyncio.to_thread(self._get_user_uid, user_id) or str(user_id)
        api_key, secret = await asyncio.to_thread(self._load_api_keys_for_user, user_id, uid)
        if api_key and secret:
            def _fetch_symbols():
                from exchange import BingXSpot
                ex = BingXSpot(api_key, secret)
                data = ex.get_popular_symbols(quote_asset="USDT", limit=20)
                return [s["symbol"] for s in data] if data else []

            try:
                api_symbols = await asyncio.to_thread(_fetch_symbols)
                for sym in popular_symbols:
                    if sym not in api_symbols:
                        api_symbols.append(sym)
            except Exception as e:
                log.warning(f"Failed to load symbols from API: {e}")
                api_symbols = popular_symbols

        # Используем API символы, если они есть, иначе популярные
        symbols_to_show = api_symbols[:20] if api_symbols else popular_symbols

        buttons = []
        for i in range(0, len(symbols_to_show), 2):
            row = []
            for j in range(2):
                if i + j < len(symbols_to_show):
                    symbol = symbols_to_show[i + j]
                    callback_data = f"symbol_{symbol.replace('-', '_')}"
                    # Помечаем текущий символ
                    label = f"✅ {symbol}" if symbol == current_symbol else symbol
                    row.append(InlineKeyboardButton(label, callback_data=callback_data))
            buttons.append(row)

        buttons.append([InlineKeyboardButton("✏️ Ввести вручную", callback_data="symbol_custom")])
        buttons.append([InlineKeyboardButton("🔄 Обновить из API", callback_data="select_symbol")])
        buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="settings")])

        keyboard = InlineKeyboardMarkup(buttons)

        symbol_source = "API BingX" if api_symbols else "предустановленный список"
        await _safe_edit_message(
            query,
            f"📌 **Выбор торговой пары**\n\n"
            f"Текущая пара: `{current_symbol}`\n"
            f"Источник: {symbol_source}\n\n"
            f"Выберите пару из списка или введите вручную:",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    async def handle_set_symbol(self, query, user_id: int, symbol: str):
        """Установить символ"""
        if symbol == "custom":
            self.user_states[user_id] = {"waiting_for_symbol": True}
            await query.edit_message_text(
                "Введите торговую пару в формате BTC-USDT:",
                reply_markup=self._get_back_keyboard(),
            )
            return

        # Получаем UID пользователя
        uid = await asyncio.to_thread(self._get_user_uid, user_id)
        if not uid:
            uid = str(user_id)  # Fallback на user_id

        # Загружаем API ключи через единый метод
        api_key, secret = await asyncio.to_thread(self._load_api_keys_for_user, user_id, uid)
        if not api_key or not secret:
            await query.edit_message_text(
                "❌ Не настроены API ключи.\n\nИспользуйте кнопку '🔑 Ввести API ключи' для настройки.", reply_markup=self._get_main_menu_keyboard()
            )
            return

        # Получаем состояние для сохранения символа (в потоке)
        state = await asyncio.to_thread(self.persistence.load_state, user_id)

        def _validate_symbol():
            ex = BingXSpot(api_key, secret)
            ex.symbol_info(symbol)

        try:
            await asyncio.to_thread(_validate_symbol)

            uid_for_bot = uid or str(user_id)
            bot = self._get_or_create_bot_for_user(uid_for_bot, user_id)
            if bot:
                old_symbol = bot.symbol
            else:
                old_symbol = state.get("symbol", config.SYMBOL) if state else config.SYMBOL

            # Если бот уже создан, отменяем все ордера по старому символу и обновляем
            if bot:
                try:
                    await bot.ex.cancel_all(bot.symbol)
                except Exception:
                    pass
                # Обновляем символ в боте
                bot.symbol = symbol
                if "-" in symbol:
                    bot.base_asset_name, bot.quote_asset_name = symbol.split("-")
                await asyncio.to_thread(bot.save_state)
                # Обновляем маппинг
                self.uid_bots[uid] = user_id
                if user_id not in self.user_bots:
                    self.user_bots[user_id] = bot

            # Сохраняем символ в состоянии
            if not state:
                state = {}
            state["symbol"] = symbol
            await asyncio.to_thread(self.persistence.save_state, user_id, state)

            await query.edit_message_text(
                f"✅ Торговая пара изменена:\n`{old_symbol}` → `{symbol}`\n\nИспользуйте 'Построить сетку' для создания новой сетки."
            )
            await asyncio.sleep(2)
            await self.show_settings_menu(query, user_id)
        except Exception as e:
            log.error(f"Error setting symbol: {e}")
            await query.edit_message_text(
                f"❌ Ошибка: {e}\n\nПроверьте, что пара {symbol} существует на BingX.",
                reply_markup=self._get_back_keyboard(),
            )

    # Админские команды
    async def cmd_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Админская панель"""
        user_id = update.effective_user.id

        if user_id != config.TG_ADMIN_ID:
            await update.message.reply_text("❌ Доступ запрещен")
            return

        await self.show_admin_menu(update.message)

    async def show_admin_menu(self, message_or_query):
        """Показать админское меню"""
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📋 Список рефералов", callback_data="admin_list_referrals"),
                    InlineKeyboardButton("⏳ Ожидающие одобрения", callback_data="admin_list_pending"),
                ],
                [
                    InlineKeyboardButton("➕ Добавить реферала", callback_data="admin_add_referral"),
                    InlineKeyboardButton("➖ Удалить реферала", callback_data="admin_remove_referral"),
                ],
            ]
        )

        referrals_count = len(await asyncio.to_thread(self.referral_system.get_all_referrals))
        text = f"👑 **Админ панель**\n\nВсего рефералов (локально): {referrals_count}"

        if hasattr(message_or_query, "reply_text"):
            await message_or_query.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await message_or_query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)

    async def cmd_set_api(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Установить API ключи (старая команда, оставлена для совместимости)"""
        await self.cmd_set_api_keys(update, context)

    async def cmd_set_api_keys(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Установить API ключи - отдельная команда"""
        user_id = update.effective_user.id

        if not await asyncio.to_thread(self._check_user_access, user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return

        self.user_states[user_id] = {"waiting_for_api": True, "api_step": "key"}
        await update.message.reply_text("🔑 **Ввод API ключей BingX**\n\nВведите ваш BingX API Key:")

    async def handle_admin_list_referrals(self, query):
        """Показать список рефералов"""
        referrals = await asyncio.to_thread(self.referral_system.get_all_referrals)

        if not referrals:
            await query.edit_message_text("📋 Список рефералов пуст")
            return

        message = "📋 **Список рефералов:**\n\n"
        for uid, info in referrals.items():
            username = info.get("username", "N/A")
            user_id = info.get("user_id", "N/A")
            registered_at = info.get("registered_at", "N/A")
            if registered_at and registered_at != "N/A":
                try:
                    from datetime import datetime

                    dt = datetime.fromisoformat(registered_at)
                    registered_at = dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass
            message += f"• UID: `{uid}`\n  User ID: {user_id}\n  Username: @{username}\n  Зарегистрирован: {registered_at}\n\n"

        keyboard = self._get_back_keyboard("admin_back")

        await query.edit_message_text(message, parse_mode="Markdown", reply_markup=keyboard)

    async def handle_admin_add_referral(self, query):
        """Добавить реферала"""
        await query.edit_message_text("Введите UID нового реферала:")
        self.user_states[query.from_user.id] = {"waiting_for_referral_uid": True, "action": "add"}

    async def handle_admin_remove_referral(self, query):
        """Удалить реферала"""
        await query.edit_message_text("Введите UID реферала для удаления:")
        self.user_states[query.from_user.id] = {"waiting_for_referral_uid": True, "action": "remove"}

    async def handle_admin_list_pending(self, query):
        """Показать ожидающих одобрения"""
        pending = await asyncio.to_thread(self.referral_system.get_pending_referrals)
        if not pending:
            await _safe_edit_message(query, "⏳ Ожидающих одобрения нет", reply_markup=self._get_back_keyboard("admin_back"))
            return
        message = "⏳ **Ожидающие одобрения:**\n\n"
        for uid, info in pending.items():
            username = info.get("username", "N/A")
            user_id = info.get("user_id", "N/A")
            requested = info.get("requested_at", "N/A")
            if requested != "N/A":
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(requested)
                    requested = dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass
            message += f"• UID: `{uid}` | @{username} | User ID: {user_id}\n  Запрос: {requested}\n\n"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]])
        await _safe_edit_message(query, message, parse_mode="Markdown", reply_markup=keyboard)

    async def handle_admin_approve_referral(self, query, uid: str, context: ContextTypes.DEFAULT_TYPE):
        """Одобрить реферала"""
        if query.from_user.id != config.TG_ADMIN_ID:
            await query.answer("❌ Нет доступа")
            return
        info = await asyncio.to_thread(self.referral_system.approve_pending_referral, uid)
        if not info:
            await query.answer("⚠️ Реферал уже одобрен или отозван")
            await _safe_edit_message(query, f"⚠️ Реферал `{uid}` уже одобрен или не найден в ожидающих.", parse_mode="Markdown")
            return
        user_id = info.get("user_id", 0)
        username = info.get("username") or ""
        # Сохраняем UID в state пользователя — тогда при /start сразу откроется меню
        state = await asyncio.to_thread(self.persistence.load_state, user_id) or {}
        state["uid"] = uid
        state["username"] = username
        await asyncio.to_thread(self.persistence.save_state, user_id, state)
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="✅ Ваш запрос одобрен! Нажмите /start для входа в меню."
            )
        except Exception as e:
            log.warning(f"Could not notify user {user_id} about approval: {e}")
        await query.answer("✅ Реферал одобрен")
        await _safe_edit_message(
            query,
            f"✅ Реферал `{uid}` одобрен.\nПользователь уведомлён.",
            parse_mode="Markdown"
        )

    async def handle_admin_reject_referral(self, query, uid: str, context: ContextTypes.DEFAULT_TYPE):
        """Отклонить реферала"""
        if query.from_user.id != config.TG_ADMIN_ID:
            await query.answer("❌ Нет доступа")
            return
        info = await asyncio.to_thread(self.referral_system.remove_pending_referral, uid)
        if not info:
            await query.answer("⚠️ Уже обработан")
            await _safe_edit_message(query, f"⚠️ Реферал `{uid}` уже обработан или не найден.", parse_mode="Markdown")
            return
        user_id = info.get("user_id", 0)
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ Ваш запрос на доступ отклонён. Обратитесь к администратору."
            )
        except Exception as e:
            log.warning(f"Could not notify user {user_id} about rejection: {e}")
        await query.answer("❌ Реферал отклонён")
        await _safe_edit_message(
            query,
            f"❌ Реферал `{uid}` отклонён.\nПользователь уведомлён.",
            parse_mode="Markdown"
        )

    async def handle_admin_list_referrals_api(self, query):
        """Показать список рефералов из API BingX"""
        user_id = query.from_user.id
        uid = await asyncio.to_thread(self._get_user_uid, user_id) or str(user_id)

        # Получаем API ключи админа через единый метод
        api_key, secret = await asyncio.to_thread(self._load_api_keys_for_user, user_id, uid)
        if not api_key or not secret:
            await query.edit_message_text(
                "❌ API ключи не настроены.\n\n"
                "Для просмотра рефералов через API необходимо настроить API ключи.\n"
                "Используйте команду /set_api_keys для настройки."
            )
            return

        try:
            await query.edit_message_text("⏳ Загружаю рефералов из API BingX...")

            def _fetch_referrals_api():
                from exchange import BingXSpot
                ex = BingXSpot(api_key, secret)
                return ex.get_referrals_from_api(page=1, page_size=100)

            referrals_data = await asyncio.to_thread(_fetch_referrals_api)

            if referrals_data is None:
                await query.edit_message_text(
                    "❌ Не удалось получить данные из API BingX.\n\n"
                    "Возможные причины:\n"
                    "• Проблемы с интернет-соединением\n"
                    "• API ключи не имеют разрешения Agent API\n"
                    "• Временные проблемы на стороне биржи"
                )
                return

            total = referrals_data.get("total", 0)
            invitees = referrals_data.get("invitees", [])

            if not invitees:
                message = "📋 **Рефералы из API BingX**\n\nРефералов не найдено."
            else:
                message = f"📋 **Рефералы из API BingX**\n\nВсего рефералов: {total}\n\n"

                for i, invitee in enumerate(invitees[:20], 1):  # Показываем первые 20
                    uid = invitee.get("inviteeUid") or invitee.get("uid") or str(invitee.get("inviteeId", "N/A"))
                    registration_time = invitee.get("registrationTime") or invitee.get("registeredAt") or invitee.get("registerTime") or "N/A"

                    # Форматируем время регистрации
                    if registration_time != "N/A":
                        try:
                            from datetime import datetime

                            # Если timestamp в миллисекундах
                            if isinstance(registration_time, (int, str)) and len(str(registration_time)) > 10:
                                try:
                                    dt = datetime.fromtimestamp(int(registration_time) / 1000)
                                except (ValueError, OSError):
                                    dt = datetime.fromtimestamp(int(registration_time))
                            elif isinstance(registration_time, str):
                                try:
                                    dt = datetime.fromisoformat(registration_time.replace("Z", "+00:00"))
                                except Exception:
                                    dt = datetime.now()
                            else:
                                dt = datetime.now()
                            registration_time = dt.strftime("%Y-%m-%d %H:%M:%S")
                        except Exception as e:
                            log.debug(f"Error formatting registration time: {e}")
                            registration_time = "N/A"

                    message += f"{i}. UID: `{uid}`\n   Зарегистрирован: {registration_time}\n\n"

                if total > 20:
                    message += f"\n... и еще {total - 20} рефералов"

            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Обновить", callback_data="admin_list_referrals_api")],
                    [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")],
                ]
            )

            await query.edit_message_text(message, parse_mode="Markdown", reply_markup=keyboard)

        except Exception as e:
            log.error(f"Error getting referrals from API: {e}")
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]])
            await query.edit_message_text(
                f"❌ Ошибка при получении рефералов из API:\n\n`{str(e)}`\n\nПроверьте API ключи и разрешения.",
                reply_markup=keyboard,
            )

    async def handle_admin_referral_commissions(self, query):
        """Показать комиссии рефералов из API"""
        user_id = query.from_user.id
        uid = await asyncio.to_thread(self._get_user_uid, user_id) or str(user_id)

        # Получаем API ключи админа через единый метод
        api_key, secret = await asyncio.to_thread(self._load_api_keys_for_user, user_id, uid)
        keyboard_back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]])
        if not api_key or not secret:
            await query.edit_message_text(
                "❌ API ключи не настроены.\n\nИспользуйте команду /set_api_keys для настройки.",
                reply_markup=keyboard_back,
            )
            return

        try:
            await query.edit_message_text("⏳ Загружаю комиссии рефералов из API...")

            from datetime import datetime, timedelta

            def _fetch_commissions():
                from exchange import BingXSpot
                ex = BingXSpot(api_key, secret)
                end_time = int(datetime.now().timestamp() * 1000)
                start_time = int((datetime.now() - timedelta(days=30)).timestamp() * 1000)
                return ex.get_referral_commissions(start_time=start_time, end_time=end_time, page=1, page_size=50)

            commissions_data = await asyncio.to_thread(_fetch_commissions)

            if commissions_data is None:
                await query.edit_message_text(
                    "❌ Не удалось получить комиссии из API BingX.",
                    reply_markup=keyboard_back,
                )
                return

            total_commission = commissions_data.get("totalCommission", "0")
            commissions = commissions_data.get("commissions", [])
            total = commissions_data.get("total", 0)

            message = f"💰 **Комиссии рефералов**\n\nПериод: последние 30 дней\nОбщая комиссия: `{total_commission} USDT`\nВсего записей: {total}\n\n"

            if commissions:
                message += "**Последние комиссии:**\n\n"
                for i, comm in enumerate(commissions[:10], 1):  # Показываем первые 10
                    invitee_uid = comm.get("inviteeUid", "N/A")
                    amount = comm.get("amount", "0")
                    commission_time = comm.get("commissionTime", "N/A")

                    # Форматируем время
                    if commission_time != "N/A":
                        try:
                            if isinstance(commission_time, (int, str)) and len(str(commission_time)) > 10:
                                dt = datetime.fromtimestamp(int(commission_time) / 1000)
                                commission_time = dt.strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            pass

                    message += f"{i}. UID: `{invitee_uid}`\n   Сумма: {amount} USDT\n   Время: {commission_time}\n\n"

                if total > 10:
                    message += f"\n... и еще {total - 10} записей"
            else:
                message += "Комиссий за этот период не найдено."

            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Обновить", callback_data="admin_referral_commissions")],
                    [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")],
                ]
            )

            await query.edit_message_text(message, parse_mode="Markdown", reply_markup=keyboard)

        except Exception as e:
            log.error(f"Error getting referral commissions: {e}")
            await query.edit_message_text(
                f"❌ Ошибка при получении комиссий:\n\n`{str(e)}`",
                reply_markup=keyboard_back,
            )

    async def handle_admin_sync_referrals(self, query):
        """Синхронизировать локальный список рефералов с API BingX"""
        user_id = query.from_user.id
        uid = await asyncio.to_thread(self._get_user_uid, user_id) or str(user_id)

        # Получаем API ключи админа через единый метод
        api_key, secret = await asyncio.to_thread(self._load_api_keys_for_user, user_id, uid)
        keyboard_back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]])
        if not api_key or not secret:
            await query.edit_message_text(
                "❌ API ключи не настроены.\n\nИспользуйте команду /set_api_keys для настройки.",
                reply_markup=keyboard_back,
            )
            return

        try:
            await query.edit_message_text("⏳ Синхронизирую рефералов с API BingX...")

            def _fetch_referrals_sync():
                from exchange import BingXSpot
                ex = BingXSpot(api_key, secret)
                return ex.get_referrals_from_api(page=1, page_size=1000)

            referrals_data = await asyncio.to_thread(_fetch_referrals_sync)

            if referrals_data is None:
                await query.edit_message_text(
                    "❌ Не удалось получить данные из API BingX.",
                    reply_markup=keyboard_back,
                )
                return

            invitees = referrals_data.get("invitees", [])

            # Синхронизируем с локальной системой (в потоке, чтобы не блокировать event loop)
            def _sync_referrals():
                a, u = 0, 0
                for invitee in invitees:
                    uid = str(invitee.get("inviteeUid", ""))
                    if not uid or uid == "N/A":
                        continue
                    if self.referral_system.is_referral(uid):
                        u += 1
                    else:
                        self.referral_system.add_referral(uid, 0, None)
                        a += 1
                return a, u, len(self.referral_system.get_all_referrals())

            added_count, updated_count, total_local = await asyncio.to_thread(_sync_referrals)

            message = (
                f"✅ **Синхронизация завершена**\n\n"
                f"Добавлено новых: {added_count}\n"
                f"Обновлено: {updated_count}\n"
                f"Всего в API: {len(invitees)}\n"
                f"Всего локально: {total_local}"
            )

            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📋 Показать список", callback_data="admin_list_referrals")],
                    [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")],
                ]
            )

            await query.edit_message_text(message, parse_mode="Markdown", reply_markup=keyboard)

        except Exception as e:
            log.error(f"Error syncing referrals: {e}")
            await query.edit_message_text(
                f"❌ Ошибка при синхронизации:\n\n`{str(e)}`",
                reply_markup=keyboard_back,
            )

    async def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Глобальный обработчик ошибок — логирует и уведомляет пользователя."""
        log.error("Exception while handling an update:", exc_info=context.error)
        if not update:
            return
        try:
            user = getattr(update, "effective_user", None) or (
                getattr(update, "callback_query", None) and getattr(update.callback_query, "from_user", None)
            ) or (getattr(update, "message", None) and getattr(update.message, "from_user", None))
            if not user:
                return
            msg = "❌ Произошла ошибка. Попробуйте /start или повторите действие позже."
            if getattr(update, "callback_query", None):
                try:
                    await update.callback_query.answer()
                    await update.callback_query.edit_message_text(msg)
                except Exception:
                    pass
            elif getattr(update, "message", None):
                await update.message.reply_text(msg)
        except Exception as e:
            log.warning(f"Error handler failed to notify user: {e}")

    def setup_handlers(self, app):
        """Настройка обработчиков"""
        self.app = app

        app.add_error_handler(self._error_handler)

        # Команды
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("admin", self.cmd_admin))
        app.add_handler(CommandHandler("set_api", self.cmd_set_api))
        app.add_handler(CommandHandler("set_api_keys", self.cmd_set_api_keys))

        # Callback обработчики
        app.add_handler(CallbackQueryHandler(self.callback_handler))

        # Обработка сообщений (UID, API ключи)
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка текстовых сообщений"""
        user_id = update.effective_user.id

        if user_id in self.user_states:
            state = self.user_states[user_id]
            branch = "waiting_for_uid" if state.get("waiting_for_uid") else "waiting_for_api" if state.get("waiting_for_api") else "waiting_for_referral_uid" if state.get("waiting_for_referral_uid") else "waiting_for_symbol" if state.get("waiting_for_symbol") else "other"
            log.info("[TG] handle_message user_id=%s branch=%s", user_id, branch)
            if state.get("waiting_for_uid"):
                await self.handle_uid_input(update, context)
            elif state.get("waiting_for_api"):
                await self.handle_api_input(update, context)
            elif state.get("waiting_for_referral_uid"):
                await self.handle_referral_uid_input(update, context)
            elif state.get("waiting_for_symbol"):
                await self.handle_symbol_input(update, context)
            log.info("[TG] handle_message user_id=%s branch=%s DONE", user_id, branch)
        else:
            log.info("[TG] handle_message user_id=%s no user_states -> reply /start", user_id)
            # Новый пользователь написал без /start — даём подсказку
            await update.message.reply_text("👋 Для начала работы отправьте команду /start")

    async def handle_referral_uid_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка ввода UID реферала (админ)"""
        user_id = update.effective_user.id

        if user_id != config.TG_ADMIN_ID:
            return

        state = self.user_states[user_id]
        uid = update.message.text.strip()
        action = state.get("action")

        if action == "add":
            ok = await asyncio.to_thread(self.referral_system.add_referral, uid, 0, update.effective_user.username)
            if ok:
                await update.message.reply_text(f"✅ Реферал {uid} добавлен")
            else:
                await update.message.reply_text(f"❌ Реферал {uid} уже существует")
        elif action == "remove":
            ok = await asyncio.to_thread(self.referral_system.remove_referral, uid)
            if ok:
                await update.message.reply_text(f"✅ Реферал {uid} удален")
            else:
                await update.message.reply_text(f"❌ Реферал {uid} не найден")

        del self.user_states[user_id]

    async def handle_symbol_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка ввода символа вручную"""
        user_id = update.effective_user.id
        symbol = update.message.text.strip().upper()

        # Получаем UID пользователя
        uid = await asyncio.to_thread(self._get_user_uid, user_id)
        if not uid:
            uid = str(user_id)  # Fallback на user_id

        # Загружаем состояние пользователя (нужно для сохранения символа, в потоке)
        state = await asyncio.to_thread(self.persistence.load_state, user_id) or {}

        # Загружаем API ключи через единый метод
        api_key, secret = await asyncio.to_thread(self._load_api_keys_for_user, user_id, uid)
        if not api_key or not secret:
            await update.message.reply_text(
                "❌ Не настроены API ключи.\n\nИспользуйте кнопку '🔑 Ввести API ключи' для настройки.", reply_markup=self._get_main_menu_keyboard()
            )
            if user_id in self.user_states:
                del self.user_states[user_id]
            return

        try:
            # Проверяем формат (должен быть BASE-QUOTE)
            if "-" not in symbol:
                await update.message.reply_text("❌ Неверный формат. Используйте формат: BTC-USDT")
                return

            def _validate_sym():
                ex = BingXSpot(api_key, secret)
                ex.symbol_info(symbol)

            await asyncio.to_thread(_validate_sym)

            # Сохраняем старый символ
            if user_id in self.user_bots:
                old_symbol = self.user_bots[user_id].symbol
            else:
                old_symbol = state.get("symbol") or config.SYMBOL

            # Если бот уже создан, отменяем все ордера по старому символу и обновляем
            if user_id in self.user_bots:
                bot = self.user_bots[user_id]
                try:
                    await bot.ex.cancel_all(bot.symbol)
                except Exception:
                    pass
                # Обновляем символ в боте
                bot.symbol = symbol
                if "-" in symbol:
                    bot.base_asset_name, bot.quote_asset_name = symbol.split("-")
                await asyncio.to_thread(bot.save_state)

            # Сохраняем символ в состоянии (в потоке)
            state["symbol"] = symbol
            await asyncio.to_thread(self.persistence.save_state, user_id, state)

            if user_id in self.user_states:
                del self.user_states[user_id]
            await update.message.reply_text(
                f"✅ Торговая пара изменена:\n`{old_symbol}` → `{symbol}`\n\nИспользуйте 'Построить сетку' для создания новой сетки."
            )
        except Exception as e:
            log.error(f"Error setting symbol: {e}")
            await update.message.reply_text(f"❌ Ошибка: {e}\n\nПроверьте, что пара {symbol} существует на BingX.")

    async def handle_set_api_keys(self, query, user_id: int):
        """Обработка нажатия кнопки 'Ввести API ключи'"""
        if not await asyncio.to_thread(self._check_user_access, user_id):
            await query.edit_message_text("❌ Доступ запрещен")
            return

        # Получаем UID пользователя
        uid = await asyncio.to_thread(self._get_user_uid, user_id)
        if not uid:
            uid = str(user_id)  # Fallback на user_id

        # Проверяем, есть ли уже сохраненные зашифрованные ключи (в потоке)
        if await asyncio.to_thread(self.persistence.has_api_keys, uid):
            # Предлагаем подтвердить перезапись или просто использовать существующие
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Использовать сохраненные", callback_data="use_saved_keys"),
                        InlineKeyboardButton("🔄 Изменить ключи", callback_data="change_api_keys"),
                    ],
                    [
                        InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu"),
                    ],
                ]
            )
            await query.edit_message_text(
                "🔑 **API ключи уже сохранены**\n\nУ вас уже есть сохраненные зашифрованные API ключи.\n\nВыберите действие:",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
            return

        # Проверяем, запущен ли бот с открытыми ордерами
        bot = self._get_or_create_bot_for_user(uid, user_id) if uid else None

        if bot:
            open_orders = [o for o in bot.orders if o.status == "open"]
            if bot.state == BotState.TRADING and open_orders:
                await query.edit_message_text(
                    "⚠️ Невозможно изменить API ключи!\n\n"
                    f"Бот запущен и работает ({len(open_orders)} открытых ордеров).\n\n"
                    "Сначала остановите бота командой 🛑 Стоп."
                )
                return

        self.user_states[user_id] = {"waiting_for_api": True, "api_step": "key"}
        await query.edit_message_text(
            "🔑 **Ввод API ключей BingX**\n\nВведите ваш BingX API Key:",
            parse_mode="Markdown",
            reply_markup=self._get_back_keyboard(),
        )

    async def handle_api_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка ввода API ключей"""
        user_id = update.effective_user.id
        text = update.message.text.strip()

        state = self.user_states.get(user_id, {})

        if state.get("api_step") == "key":
            state["api_key"] = text
            state["api_step"] = "secret"
            await update.message.reply_text("Введите ваш BingX Secret:")
            return
        elif state.get("api_step") == "secret":
            state["secret"] = text
            state["waiting_for_api"] = False

            api_key = state.get("api_key")
            api_secret = state.get("secret")

            if not api_key or not api_secret:
                await update.message.reply_text("❌ Ошибка: не получены оба ключа. Попробуйте снова: /set_api")
                if user_id in self.user_states:
                    del self.user_states[user_id]
                return

            # Проверяем ключи перед сохранением (синхронный HTTP — в потоке, чтобы не блокировать бота)
            def _check_keys():
                ex = BingXSpot(api_key, api_secret)
                ex.balance("USDT")
                ex.circuit_breaker.reset()

            try:
                await asyncio.to_thread(_check_keys)
            except Exception as e:
                error_msg = str(e)
                if "Incorrect apiKey" in error_msg or "api key" in error_msg.lower():
                    await update.message.reply_text(
                        "❌ Неверный API ключ или секрет!\n\n"
                        "Проверьте правильность введенных данных.\n"
                        "Убедитесь, что API ключ имеет разрешение 'Spot Trading'.\n\n"
                        "Попробуйте снова: /set_api"
                    )
                    state["waiting_for_api"] = True
                    state["api_step"] = "key"
                    return
                else:
                    log.warning(f"Error checking API keys: {e}")

            # Получаем UID пользователя для сохранения ключей
            uid = await asyncio.to_thread(self._get_user_uid, user_id)
            if not uid:
                # Если UID нет, используем user_id как UID (для обратной совместимости)
                uid = str(user_id)
                # Сохраняем user_id как UID (в потоке)
                user_state = await asyncio.to_thread(self.persistence.load_state, user_id) or {}
                user_state["uid"] = uid
                await asyncio.to_thread(self.persistence.save_state, user_id, user_state)

            # Сохраняем API ключи с шифрованием по UID (в потоке — не блокировать event loop)
            try:
                await asyncio.to_thread(self.persistence.save_api_keys, uid, api_key, api_secret)
                log.info(f"API keys saved (encrypted) for UID {uid}")
            except Exception as e:
                log.error(f"Error saving encrypted API keys for UID {uid}: {e}")
                await update.message.reply_text(f"❌ Ошибка при сохранении ключей: {e}\n\nПопробуйте снова: /set_api")
                if user_id in self.user_states:
                    del self.user_states[user_id]
                return

            # Если бот уже запущен, останавливаем его для применения новых ключей
            if user_id in self.user_bots:
                bot = self.user_bots[user_id]
                # Останавливаем старый бот
                bot.state = BotState.PAUSED
                try:
                    await bot.ex.cancel_all(bot.symbol)
                except Exception:
                    pass
                # Удаляем старый бот (будет пересоздан при следующем запуске)
                del self.user_bots[user_id]
                # Удаляем из маппинга UID
                if uid in self.uid_bots:
                    del self.uid_bots[uid]

            if user_id in self.user_states:
                del self.user_states[user_id]

            await update.message.reply_text("✅ API ключи сохранены и проверены!\n\nТеперь вы можете запустить бота через главное меню: /start")

    async def handle_use_saved_keys(self, query, user_id: int):
        """Использовать сохраненные API ключи"""
        uid = await asyncio.to_thread(self._get_user_uid, user_id)
        if not uid:
            uid = str(user_id)

        if not await asyncio.to_thread(self.persistence.has_api_keys, uid):
            await query.edit_message_text(
                "❌ Сохраненные ключи не найдены.\n\nИспользуйте кнопку '🔄 Изменить ключи' для ввода новых ключей.",
                reply_markup=self._get_back_keyboard(),
            )
            return

        await query.edit_message_text(
            "✅ Используются сохраненные зашифрованные API ключи.\n\nТеперь вы можете запустить бота через главное меню: /start",
            reply_markup=self._get_back_keyboard(),
        )

    async def handle_change_api_keys(self, query, user_id: int):
        """Изменить API ключи (запросить новые)"""
        uid = await asyncio.to_thread(self._get_user_uid, user_id)
        if not uid:
            uid = str(user_id)

        # Проверяем, запущен ли бот с открытыми ордерами
        bot = self._get_or_create_bot_for_user(uid, user_id) if uid else None

        if bot:
            open_orders = [o for o in bot.orders if o.status == "open"]
            if bot.state == BotState.TRADING and open_orders:
                await query.edit_message_text(
                    "⚠️ Невозможно изменить API ключи!\n\n"
                    f"Бот запущен и работает ({len(open_orders)} открытых ордеров).\n\n"
                    "Сначала остановите бота командой 🛑 Стоп.",
                    reply_markup=self._get_back_keyboard(),
                )
                return

        # Включаем режим ввода API ключей
        self.user_states[user_id] = {"waiting_for_api": True, "api_step": "key"}
        await query.edit_message_text(
            "🔑 **Ввод новых API ключей BingX**\n\nВведите ваш BingX API Key:",
            parse_mode="Markdown",
            reply_markup=self._get_back_keyboard(),
        )

    def initialize(self):
        """Инициализация Telegram бота"""
        self.app = ApplicationBuilder().token(config.TG_TOKEN).connect_timeout(30).read_timeout(30).build()

        self.setup_handlers(self.app)
        return self.app
