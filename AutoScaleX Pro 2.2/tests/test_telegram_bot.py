"""
Unit tests for telegram_bot — _is_error_notification, _safe_edit_message, handlers, e2e
"""

import os
import sys
import tempfile
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from referral_system import ReferralSystem
    from telegram_bot import (
        _is_error_notification,
        _safe_edit_message,
        _is_success_or_info,
        _should_send_error_to_user,
        TelegramBotManager,
    )
    from trading_bot import BotState
except ImportError:
    ReferralSystem = None
    _is_error_notification = None
    _safe_edit_message = None
    _is_success_or_info = None
    _should_send_error_to_user = None
    TelegramBotManager = None
    BotState = None

pytestmark = pytest.mark.skipif(
    _is_error_notification is None or _safe_edit_message is None,
    reason="telegram_bot module not available",
)


def _make_manager_with_temp_referrals():
    """Создать менеджер с временным файлом рефералов."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    mgr = TelegramBotManager()
    mgr.referral_system = ReferralSystem(referrals_file=path)
    mgr._temp_referrals_path = path
    return mgr


def _cleanup_temp_referrals(mgr):
    """Удалить временный файл рефералов."""
    path = getattr(mgr, "_temp_referrals_path", None)
    if path and os.path.exists(path):
        try:
            pending = os.path.join(os.path.dirname(path), "pending_referrals.json")
            if os.path.exists(pending):
                os.remove(pending)
        except OSError:
            pass
        try:
            os.remove(path)
        except OSError:
            pass


class TestIsErrorNotification:
    """Тесты _is_error_notification — определение критических уведомлений для троттлинга"""

    def test_emoji_critical(self):
        assert _is_error_notification("🚨 Ошибка") is True
        assert _is_error_notification("⚠️ Предупреждение") is True

    def test_critical_error_text(self):
        assert _is_error_notification("Критическая ошибка API") is True
        assert _is_error_notification("Что-то: Критическая ошибка") is True

    def test_circuit_breaker(self):
        assert _is_error_notification("Circuit breaker открыт") is True

    def test_api_key_error(self):
        assert _is_error_notification("Ошибка API ключа!") is True

    def test_timeout(self):
        assert _is_error_notification("Превышено время ожидания") is True

    def test_normal_message_not_error(self):
        assert _is_error_notification("✅ Торговля возобновлена") is False
        assert _is_error_notification("Баланс: 100 USDT") is False
        assert _is_error_notification("") is False
        assert _is_error_notification("   ") is False


@pytest.mark.asyncio
class TestSafeEditMessage:
    """Тесты _safe_edit_message — игнорирование 'message is not modified'"""

    async def test_success_edit(self):
        mock_obj = AsyncMock()
        await _safe_edit_message(mock_obj, "Новый текст")
        mock_obj.edit_message_text.assert_called_once_with("Новый текст")

    async def test_ignores_message_not_modified(self):
        from telegram.error import BadRequest

        mock_obj = AsyncMock()
        mock_obj.edit_message_text.side_effect = BadRequest("message is not modified")
        await _safe_edit_message(mock_obj, "Текст")
        mock_obj.edit_message_text.assert_called_once()

    async def test_reraises_other_bad_request(self):
        from telegram.error import BadRequest

        mock_obj = AsyncMock()
        mock_obj.edit_message_text.side_effect = BadRequest("chat not found")
        with pytest.raises(BadRequest):
            await _safe_edit_message(mock_obj, "Текст")


class TestIsSuccessOrInfo:
    def test_success_emoji(self):
        assert _is_success_or_info("✅ Готово") is True

    def test_bot_restored(self):
        assert _is_success_or_info("Бот восстановлен") is True

    def test_normal_message_false(self):
        assert _is_success_or_info("Баланс 100") is False


class TestShouldSendErrorToUser:
    def test_delegates_to_is_telegram_critical(self):
        assert _should_send_error_to_user("Circuit breaker") is True
        assert _should_send_error_to_user("order not exist") is False


class TestTelegramBotManager:
    def test_get_back_keyboard(self):
        mgr = TelegramBotManager()
        kb = mgr._get_back_keyboard()
        assert kb is not None
        assert kb.inline_keyboard
        assert kb.inline_keyboard[0][0].callback_data == "back_to_menu"

    def test_get_back_keyboard_admin(self):
        mgr = TelegramBotManager()
        kb = mgr._get_back_keyboard("admin_back")
        assert kb.inline_keyboard[0][0].callback_data == "admin_back"

    def test_get_bot_by_uid_empty(self):
        mgr = TelegramBotManager()
        assert mgr._get_bot_by_uid("any") is None

    def test_get_or_create_bot_for_user_empty(self):
        mgr = TelegramBotManager()
        assert mgr._get_or_create_bot_for_user("uid1", 123) is None

    def test_get_user_uid_fallback(self):
        mgr = TelegramBotManager()
        uid = mgr._get_user_uid(99999)
        assert uid == "99999"


# --- Handlers and e2e (mocked) ---


@pytest.mark.asyncio
class TestCmdStartHandler:
    """cmd_start handler: saved UID -> main menu; no UID + admin -> main menu; no UID + not admin -> ask UID."""

    async def test_cmd_start_with_saved_uid_shows_main_menu(self):
        mgr = TelegramBotManager()
        mgr.persistence.load_state = lambda uid: {"uid": "12345", "username": "user"}
        update = AsyncMock()
        update.effective_user.id = 100
        update.effective_user.username = "user"
        update.message.reply_text = AsyncMock()
        context = AsyncMock()

        with patch.object(mgr, "show_main_menu", new_callable=AsyncMock) as show_menu:
            with patch.object(mgr.referral_system, "is_referral", return_value=True):
                await mgr.cmd_start(update, context)
            show_menu.assert_called_once_with(update, context)

    async def test_cmd_start_admin_without_uid_shows_main_menu(self):
        import config
        mgr = TelegramBotManager()
        mgr.persistence.load_state = lambda uid: {}
        update = AsyncMock()
        update.effective_user.id = getattr(config, "TG_ADMIN_ID", 999999)
        update.effective_user.username = "admin"
        update.message.reply_text = AsyncMock()
        context = AsyncMock()

        with patch.object(mgr, "show_main_menu", new_callable=AsyncMock) as show_menu:
            await mgr.cmd_start(update, context)
            show_menu.assert_called_once()

    async def test_cmd_start_no_uid_not_admin_asks_for_uid(self):
        mgr = TelegramBotManager()
        mgr.persistence.load_state = lambda uid: {}
        update = AsyncMock()
        update.effective_user.id = 12345
        update.effective_user.username = "user"
        update.message.reply_text = AsyncMock()
        context = AsyncMock()

        with patch("telegram_bot.config") as mock_config:
            mock_config.TG_ADMIN_ID = 999999
            await mgr.cmd_start(update, context)
            update.message.reply_text.assert_called_once()
            call_text = update.message.reply_text.call_args[0][0]
            assert "UID" in call_text or "Добро пожаловать" in call_text


@pytest.mark.asyncio
class TestCallbackHandler:
    """callback_handler routes to correct handler."""

    async def test_callback_back_to_menu_edits_message(self):
        mgr = TelegramBotManager()
        mgr.persistence.load_state = lambda uid: {"uid": "test-uid"}
        query = AsyncMock()
        query.data = "back_to_menu"
        query.from_user.id = 100
        query.edit_message_text = AsyncMock()
        update = AsyncMock()
        update.callback_query = query
        context = AsyncMock()

        with patch.object(mgr.referral_system, "is_referral", return_value=True):
            await mgr.callback_handler(update, context)
        query.answer.assert_called_once()
        query.edit_message_text.assert_called_once()
        assert "Главное меню" in query.edit_message_text.call_args[0][0]


@pytest.mark.asyncio
class TestE2ETelegramFlow:
    """Minimal e2e: manager initialized, /start path with mocks."""

    async def test_start_then_main_menu_reply_markup(self):
        mgr = TelegramBotManager()
        mgr.persistence.load_state = lambda uid: {"uid": "123"}
        mgr.persistence.save_state = lambda uid, s: None
        update = AsyncMock()
        update.effective_user.id = 1
        update.effective_user.username = "u"
        update.message.reply_text = AsyncMock()
        context = AsyncMock()

        with patch.object(mgr, "show_main_menu", new_callable=AsyncMock) as show:
            with patch.object(mgr.referral_system, "is_referral", return_value=True):
                await mgr.cmd_start(update, context)
            show.assert_called_once()
            # main menu sends message with reply_markup (inline keyboard)
            args, kwargs = show.call_args
            assert args[0] is update
            assert args[1] is context


# --- handle_message ---


@pytest.mark.asyncio
class TestHandleMessage:
    """handle_message — роутинг и ответ новым пользователям."""

    async def test_user_not_in_states_receives_start_prompt(self):
        """Новый пользователь без /start получает подсказку."""
        mgr = TelegramBotManager()
        update = AsyncMock()
        update.effective_user.id = 99999
        update.message.reply_text = AsyncMock()
        context = AsyncMock()

        await mgr.handle_message(update, context)

        update.message.reply_text.assert_called_once()
        call_text = update.message.reply_text.call_args[0][0]
        assert "/start" in call_text

    async def test_user_waiting_uid_calls_handle_uid_input(self):
        """Пользователь в ожидании UID — вызывается handle_uid_input."""
        mgr = TelegramBotManager()
        mgr.user_states[100] = {"waiting_for_uid": True}
        update = AsyncMock()
        update.effective_user.id = 100
        update.message.text = "12345"
        update.message.reply_text = AsyncMock()
        update.effective_user.username = "user"
        context = AsyncMock()

        with patch.object(mgr, "handle_uid_input", new_callable=AsyncMock) as handle_uid:
            await mgr.handle_message(update, context)
            handle_uid.assert_called_once_with(update, context)


# --- handle_uid_input ---


@pytest.mark.asyncio
class TestHandleUidInput:
    """handle_uid_input — ввод UID, рефералы, pending."""

    async def test_admin_uid_saved_and_main_menu(self):
        """Админ вводит UID — сохраняется, показывается меню."""
        mgr = _make_manager_with_temp_referrals()
        try:
            mgr.user_states[111] = {"waiting_for_uid": True}
            mgr.persistence.load_state = lambda uid: {}
            mgr.persistence.save_state = MagicMock()

            update = AsyncMock()
            update.effective_user.id = 111
            update.effective_user.username = "admin"
            update.message.text = "999"
            update.message.reply_text = AsyncMock()
            context = AsyncMock()

            with patch("telegram_bot.config") as mock_config:
                mock_config.TG_ADMIN_ID = 111
                with patch.object(mgr, "show_main_menu", new_callable=AsyncMock) as show:
                    await mgr.handle_uid_input(update, context)
                    show.assert_called_once()
                    assert 111 not in mgr.user_states
                    call_text = update.message.reply_text.call_args_list[0][0][0]
                    assert "UID сохранен" in call_text
        finally:
            _cleanup_temp_referrals(mgr)

    async def test_approved_referral_uid_saved_and_main_menu(self):
        """Одобренный реферал вводит UID — сохраняется, показывается меню."""
        mgr = _make_manager_with_temp_referrals()
        try:
            mgr.referral_system.add_referral("ref123", 0, None)
            mgr.user_states[222] = {"waiting_for_uid": True}
            mgr.persistence.load_state = lambda uid: {}
            mgr.persistence.save_state = MagicMock()

            update = AsyncMock()
            update.effective_user.id = 222
            update.effective_user.username = "refuser"
            update.message.text = "ref123"
            update.message.reply_text = AsyncMock()
            context = AsyncMock()

            with patch("telegram_bot.config") as mock_config:
                mock_config.TG_ADMIN_ID = 999999
                with patch.object(mgr, "show_main_menu", new_callable=AsyncMock) as show:
                    await mgr.handle_uid_input(update, context)
                    show.assert_called_once()
                    assert 222 not in mgr.user_states
                    call_text = update.message.reply_text.call_args_list[0][0][0]
                    assert "Реферал подтвержден" in call_text
        finally:
            _cleanup_temp_referrals(mgr)

    async def test_new_uid_adds_pending_and_notifies_user(self):
        """Новый UID — добавляется в pending, пользователю — ожидание одобрения."""
        mgr = _make_manager_with_temp_referrals()
        try:
            mgr.user_states[333] = {"waiting_for_uid": True}
            mgr.persistence.load_state = lambda uid: {}

            update = AsyncMock()
            update.effective_user.id = 333
            update.effective_user.username = "newuser"
            update.message.text = "newuid456"
            update.message.date.strftime = MagicMock(return_value="2026-02-22 12:00:00")
            update.message.reply_text = AsyncMock()
            context = AsyncMock()
            context.bot.send_message = AsyncMock()

            with patch("telegram_bot.config") as mock_config:
                mock_config.TG_ADMIN_ID = 999999
                await mgr.handle_uid_input(update, context)

            assert mgr.referral_system.get_pending_referrals().get("newuid456") is not None
            assert 333 not in mgr.user_states
            call_text = update.message.reply_text.call_args_list[0][0][0]
            assert "одобрение" in call_text or "ожидайте" in call_text.lower()
        finally:
            _cleanup_temp_referrals(mgr)


# --- Admin handlers ---


@pytest.mark.asyncio
class TestAdminHandlers:
    """admin approve/reject, list_pending."""

    async def test_admin_approve_referral_notifies_user(self):
        """Одобрение реферала — пользователь уведомляется."""
        mgr = _make_manager_with_temp_referrals()
        try:
            mgr.referral_system.add_pending_referral("uid777", 444, "approved_user")

            query = AsyncMock()
            query.from_user.id = 999999
            query.answer = AsyncMock()

            context = AsyncMock()
            context.bot.send_message = AsyncMock()

            with patch("telegram_bot.config") as mock_config:
                mock_config.TG_ADMIN_ID = 999999
                with patch("telegram_bot._safe_edit_message", new_callable=AsyncMock):
                    await mgr.handle_admin_approve_referral(query, "uid777", context)

            assert mgr.referral_system.is_referral("uid777") is True
            context.bot.send_message.assert_called_once()
            assert context.bot.send_message.call_args[1]["chat_id"] == 444
            assert "одобрен" in context.bot.send_message.call_args[1]["text"].lower()
        finally:
            _cleanup_temp_referrals(mgr)

    async def test_admin_reject_referral_removes_pending(self):
        """Отклонение реферала — удаляется из pending."""
        mgr = _make_manager_with_temp_referrals()
        try:
            mgr.referral_system.add_pending_referral("uid888", 555, "rejected_user")

            query = AsyncMock()
            query.from_user.id = 999999
            query.answer = AsyncMock()

            context = AsyncMock()
            context.bot.send_message = AsyncMock()

            with patch("telegram_bot.config") as mock_config:
                mock_config.TG_ADMIN_ID = 999999
                with patch("telegram_bot._safe_edit_message", new_callable=AsyncMock):
                    await mgr.handle_admin_reject_referral(query, "uid888", context)

            assert mgr.referral_system.get_pending_referral("uid888") is None
            context.bot.send_message.assert_called_once()
            assert "отклон" in context.bot.send_message.call_args[1]["text"].lower()
        finally:
            _cleanup_temp_referrals(mgr)

    async def test_admin_list_pending_empty(self):
        """Список ожидающих пуст — сообщение об этом."""
        mgr = TelegramBotManager()
        query = AsyncMock()
        query.edit_message_text = AsyncMock()

        with patch("telegram_bot._safe_edit_message", new_callable=AsyncMock) as safe_edit:
            await mgr.handle_admin_list_pending(query)
            safe_edit.assert_called_once()
            call_args = safe_edit.call_args
            assert "Ожидающих одобрения нет" in call_args[0][1] or "нет" in str(call_args).lower()


# --- Main menu keyboard ---


class TestMainMenuKeyboard:
    """Проверка клавиатуры главного меню."""

    def test_main_menu_has_start_pause_stop(self):
        """Главное меню содержит кнопки Старт, Пауза, Стоп."""
        mgr = TelegramBotManager()
        kb = mgr._get_main_menu_keyboard()
        flat = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert "start_bot" in flat
        assert "pause_bot" in flat
        assert "stop_bot" in flat

    def test_main_menu_has_settings(self):
        """Главное меню содержит кнопку настроек/API."""
        mgr = TelegramBotManager()
        kb = mgr._get_main_menu_keyboard()
        flat = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert "set_api_keys" in flat


@pytest.mark.asyncio
class TestSettingsMenuOrderValues:
    """Кнопки размера ордера в настройках (в т.ч. 35 USDT)."""

    async def test_settings_menu_has_order_value_35(self):
        """Меню настроек содержит кнопку «Ордер 35 USDT»."""
        mgr = TelegramBotManager()
        state = {"uid": "123", "symbol": "BTC-USDT", "grid_step_pct": 0.0075}
        mgr.persistence.load_state = lambda uid: state
        query = AsyncMock()
        query.edit_message_text = AsyncMock()

        async def to_thread(fn, *args):
            if getattr(fn, "__name__", "") == "_load_api_keys_for_user":
                return ("key", "secret")
            return fn(*args)

        with patch("telegram_bot.asyncio.to_thread", new_callable=AsyncMock, side_effect=to_thread):
            await mgr.show_settings_menu(query, 100)

        call_kw = query.edit_message_text.call_args[1]
        kb = call_kw.get("reply_markup")
        assert kb is not None
        flat = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert "order_value_35" in flat

    async def test_callback_order_value_35_calls_handler_with_35(self):
        """При нажатии «Ордер 35 USDT» вызывается handle_set_order_value с value=35."""
        mgr = TelegramBotManager()
        mgr.persistence.load_state = lambda uid: {"uid": "123"}
        query = AsyncMock()
        query.data = "order_value_35"
        query.from_user.id = 100
        query.edit_message_text = AsyncMock()
        update = AsyncMock()
        update.callback_query = query
        context = AsyncMock()

        with patch.object(mgr, "handle_set_order_value", new_callable=AsyncMock) as handle_order:
            with patch.object(mgr.referral_system, "is_referral", return_value=True):
                await mgr.callback_handler(update, context)
        handle_order.assert_called_once()
        assert handle_order.call_args[0][2] == 35


# --- handle_balance ---


@pytest.mark.asyncio
class TestHandleBalance:
    """Тесты handle_balance — экран «💰 Баланс», initial_equity и Profit Bank."""

    async def test_balance_sets_initial_equity_when_zero_and_saves_state(self):
        """При initial_equity=0 выставляется базовый уровень (total_equity) и вызывается save_state."""
        mgr = TelegramBotManager()
        query = AsyncMock()
        query.edit_message_text = AsyncMock()

        mock_bot = MagicMock()
        mock_bot.initial_equity = Decimal("0")
        mock_bot.profit_bank = Decimal("18.62")
        mock_bot.quote_asset_name = "USDT"
        mock_bot.base_asset_name = "KSM"
        mock_bot.symbol = "KSM-USDT"
        mock_bot.load_state = MagicMock()
        mock_bot.save_state = MagicMock()
        mock_bot.get_current_price = AsyncMock(return_value=Decimal("4.51"))
        mock_bot.get_total_equity = AsyncMock(return_value=Decimal("1295.07"))
        mock_bot.ex = MagicMock()
        mock_bot.ex.balance = AsyncMock(side_effect=[Decimal("1085.58"), Decimal("46.449665")])

        def run_in_thread(fn, *args):
            return fn(*args)

        with patch.object(mgr, "_get_user_uid", return_value="uid1"):
            with patch.object(mgr, "_get_or_create_bot_for_user", return_value=mock_bot):
                with patch("telegram_bot.asyncio.to_thread", new_callable=AsyncMock, side_effect=run_in_thread):
                    with patch("telegram_bot._safe_edit_message", new_callable=AsyncMock) as safe_edit:
                        await mgr.handle_balance(query, 123)

        mock_bot.load_state.assert_called_once_with(True)
        assert mock_bot.initial_equity == Decimal("1295.07")
        mock_bot.save_state.assert_called_once()
        safe_edit.assert_called_once()
        msg = safe_edit.call_args[0][1]
        assert "Прибыль: `0.00" in msg
        assert "Profit Bank: `18.62" in msg
        assert "1295.07" in msg

    async def test_balance_shows_profit_and_roi_when_initial_equity_set(self):
        """При initial_equity > 0 в сообщении считаются прибыль и ROI."""
        mgr = TelegramBotManager()
        query = AsyncMock()

        mock_bot = MagicMock()
        mock_bot.initial_equity = Decimal("1000")
        mock_bot.profit_bank = Decimal("5.00")
        mock_bot.quote_asset_name = "USDT"
        mock_bot.base_asset_name = "ETH"
        mock_bot.symbol = "ETH-USDT"
        mock_bot.load_state = MagicMock()
        mock_bot.save_state = MagicMock()
        mock_bot.get_current_price = AsyncMock(return_value=Decimal("2000"))
        mock_bot.get_total_equity = AsyncMock(return_value=Decimal("1100"))
        mock_bot.ex = MagicMock()
        mock_bot.ex.balance = AsyncMock(side_effect=[Decimal("100"), Decimal("0.5")])

        with patch.object(mgr, "_get_user_uid", return_value="uid1"):
            with patch.object(mgr, "_get_or_create_bot_for_user", return_value=mock_bot):
                with patch("telegram_bot.asyncio.to_thread", new_callable=AsyncMock, side_effect=lambda fn, *a: fn(*a)):
                    with patch("telegram_bot._safe_edit_message", new_callable=AsyncMock) as safe_edit:
                        await mgr.handle_balance(query, 456)

        msg = safe_edit.call_args[0][1]
        assert "Прибыль: `100.00" in msg
        assert "10.00%" in msg
        assert "Profit Bank: `5.00" in msg

    async def test_balance_calls_load_state_before_building_message(self):
        """Перед построением сообщения вызывается bot.load_state(skip_bot_state=True), чтобы не останавливать работающий бот."""
        mgr = TelegramBotManager()
        query = AsyncMock()

        mock_bot = MagicMock()
        mock_bot.initial_equity = Decimal("500")
        mock_bot.profit_bank = Decimal("0")
        mock_bot.quote_asset_name = "USDT"
        mock_bot.base_asset_name = "DOT"
        mock_bot.symbol = "DOT-USDT"
        mock_bot.load_state = MagicMock()
        mock_bot.save_state = MagicMock()
        mock_bot.get_current_price = AsyncMock(return_value=Decimal("8"))
        mock_bot.get_total_equity = AsyncMock(return_value=Decimal("600"))
        mock_bot.ex = MagicMock()
        mock_bot.ex.balance = AsyncMock(side_effect=[Decimal("200"), Decimal("50")])

        with patch.object(mgr, "_get_user_uid", return_value="uid1"):
            with patch.object(mgr, "_get_or_create_bot_for_user", return_value=mock_bot):
                with patch("telegram_bot.asyncio.to_thread", new_callable=AsyncMock, side_effect=lambda fn, *a: fn(*a)):
                    with patch("telegram_bot._safe_edit_message", new_callable=AsyncMock):
                        await mgr.handle_balance(query, 789)

        mock_bot.load_state.assert_called_once_with(True)

    async def test_balance_profit_bank_from_state_same_as_pyramiding(self):
        """Profit Bank в «Баланс» — то же значение из state, что используется для пирамидинга."""
        mgr = TelegramBotManager()
        query = AsyncMock()

        mock_bot = MagicMock()
        mock_bot.initial_equity = Decimal("1000")
        mock_bot.profit_bank = Decimal("18.38")  # из state = то, от чего срабатывает пирамидинг
        mock_bot.quote_asset_name = "USDT"
        mock_bot.base_asset_name = "KSM"
        mock_bot.symbol = "KSM-USDT"
        mock_bot.load_state = MagicMock()
        mock_bot.save_state = MagicMock()
        mock_bot.get_current_price = AsyncMock(return_value=Decimal("4.51"))
        mock_bot.get_total_equity = AsyncMock(return_value=Decimal("1295"))
        mock_bot.ex = MagicMock()
        mock_bot.ex.balance = AsyncMock(side_effect=[Decimal("1085"), Decimal("46")])

        def run_in_thread(fn, *args):
            return fn(*args)

        with patch.object(mgr, "_get_user_uid", return_value="uid1"):
            with patch.object(mgr, "_get_or_create_bot_for_user", return_value=mock_bot):
                with patch("telegram_bot.asyncio.to_thread", new_callable=AsyncMock, side_effect=run_in_thread):
                    with patch("telegram_bot._safe_edit_message", new_callable=AsyncMock) as safe_edit:
                        await mgr.handle_balance(query, 111)

        msg = safe_edit.call_args[0][1]
        assert "Profit Bank: `18.38" in msg
        assert "для пирамидинга" in msg

    async def test_balance_profit_bank_shows_state_value(self):
        """Profit Bank отображает bot.profit_bank из state."""
        mgr = TelegramBotManager()
        query = AsyncMock()

        mock_bot = MagicMock()
        mock_bot.initial_equity = Decimal("500")
        mock_bot.profit_bank = Decimal("12.34")
        mock_bot.quote_asset_name = "USDT"
        mock_bot.base_asset_name = "DOT"
        mock_bot.symbol = "DOT-USDT"
        mock_bot.load_state = MagicMock()
        mock_bot.save_state = MagicMock()
        mock_bot.get_current_price = AsyncMock(return_value=Decimal("8"))
        mock_bot.get_total_equity = AsyncMock(return_value=Decimal("600"))
        mock_bot.ex = MagicMock()
        mock_bot.ex.balance = AsyncMock(side_effect=[Decimal("100"), Decimal("62.5")])

        def run_in_thread(fn, *args):
            return fn(*args)

        with patch.object(mgr, "_get_user_uid", return_value="uid1"):
            with patch.object(mgr, "_get_or_create_bot_for_user", return_value=mock_bot):
                with patch("telegram_bot.asyncio.to_thread", new_callable=AsyncMock, side_effect=run_in_thread):
                    with patch("telegram_bot._safe_edit_message", new_callable=AsyncMock) as safe_edit:
                        await mgr.handle_balance(query, 222)

        msg = safe_edit.call_args[0][1]
        assert "Profit Bank: `12.34" in msg
        assert "для пирамидинга" in msg


@pytest.mark.asyncio
class TestHandleBuildGridResetsProfitWhenNewGrid:
    """Тесты handle_build_grid: сброс profit_bank и счётчиков при построении новой сетки (смена монеты)."""

    async def test_build_grid_resets_profit_bank_and_counters_when_creating_new_grid(self):
        """При построении новой сетки (нет ордеров на бирже) profit_bank, initial_equity и счётчики сбрасываются."""
        mgr = TelegramBotManager()
        query = AsyncMock()
        query.edit_message_text = AsyncMock()
        user_id = 508265586

        mock_bot = MagicMock()
        mock_bot.profit_bank = Decimal("10")
        mock_bot.initial_equity = Decimal("100")
        mock_bot.total_executed_buys = 5
        mock_bot.total_executed_sells = 3
        mock_bot.orders = []
        mock_bot.symbol = "AVAX-USDT"
        mock_bot.grid_step_pct = Decimal("0.015")
        mock_bot.buy_order_value = Decimal("20")
        mock_bot.ex = MagicMock()
        mock_bot.ex.open_orders = AsyncMock(return_value=[])
        mock_bot.create_grid = AsyncMock()
        mock_bot.save_state = MagicMock()

        async def to_thread_impl(fn, *args):
            return fn(*args)

        with patch.object(mgr, "_get_user_uid", return_value="35176918"):
            with patch.object(mgr, "_get_or_create_bot_for_user", return_value=None):
                with patch.object(mgr, "_load_api_keys_for_user", return_value=("key", "secret")):
                    with patch.object(mgr.persistence, "load_state", return_value={"symbol": "AVAX-USDT", "grid_step_pct": "0.015", "buy_order_value": "20"}):
                        with patch("telegram_bot._create_trading_bot", return_value=mock_bot):
                            with patch("telegram_bot.asyncio.to_thread", new_callable=AsyncMock, side_effect=to_thread_impl):
                                with patch("telegram_bot.asyncio.create_task", new_callable=MagicMock):
                                    await mgr.handle_build_grid(query, user_id)

        assert mock_bot.profit_bank == Decimal("0")
        assert mock_bot.initial_equity == Decimal("0")
        assert mock_bot.total_executed_buys == 0
        assert mock_bot.total_executed_sells == 0
        mock_bot.create_grid.assert_called_once()
        assert mock_bot.save_state.call_count >= 1


@pytest.mark.asyncio
class TestHandleStartBotResumeAndBalance:
    """Тесты handle_start_bot: подхват существующей сетки при наличии ордеров на бирже; баланс/настройки при отсутствии."""

    async def test_handle_start_bot_resumes_when_exchange_has_orders(self):
        """При нажатии Старт и наличии ордеров на бирже бот подхватывает сетку и показывает «Бот запущен с существующей сеткой»."""
        mgr = TelegramBotManager()
        query = AsyncMock()
        query.edit_message_text = AsyncMock()

        mock_bot = MagicMock()
        mock_bot.state = BotState.STOPPED
        mock_bot.symbol = "DOT-USDT"
        mock_bot.orders = []
        mock_bot.ex = MagicMock()
        mock_bot.ex.open_orders = AsyncMock(return_value=[
            {"orderId": "o1", "side": "BUY", "price": "1.17"},
            {"orderId": "o2", "side": "BUY", "price": "1.15"},
        ])
        mock_bot.load_state = MagicMock()
        async def _sync_add_orders():
            mock_bot.orders.extend([
                MagicMock(side="BUY", status="open"),
                MagicMock(side="BUY", status="open"),
            ])
        mock_bot.sync_orders_from_exchange = AsyncMock(side_effect=_sync_add_orders)

        def run_in_thread(fn, *args):
            return fn(*args) if args else fn()

        with patch.object(mgr, "_get_user_uid", return_value="uid1"):
            with patch.object(mgr, "_get_or_create_bot_for_user", return_value=mock_bot):
                with patch.object(mgr, "_load_api_keys_for_user", return_value=("key", "secret")):
                    with patch.object(mgr.persistence, "load_state", return_value={"symbol": "DOT-USDT"}):
                        with patch("telegram_bot.asyncio.to_thread", new_callable=AsyncMock, side_effect=run_in_thread):
                            with patch("telegram_bot._safe_edit_message", new_callable=AsyncMock) as safe_edit:
                                with patch("telegram_bot.asyncio.create_task", new_callable=MagicMock) as create_task:
                                    await mgr.handle_start_bot(query, 100)

        mock_bot.ex.open_orders.assert_called_once()
        mock_bot.sync_orders_from_exchange.assert_called_once()
        create_task.assert_called_once()
        assert mock_bot.state == BotState.INITIALIZING
        msg = safe_edit.call_args[0][1]
        assert "Бот запущен с существующей сеткой" in msg or "существующей сеткой" in msg

    async def test_handle_start_bot_shows_balance_when_no_orders_on_exchange(self):
        """При нажатии Старт и отсутствии ордеров на бирже показываются баланс и настройки (не подхват)."""
        mgr = TelegramBotManager()
        query = AsyncMock()
        query.edit_message_text = AsyncMock()

        mock_bot = MagicMock()
        mock_bot.state = BotState.STOPPED
        mock_bot.symbol = "DOT-USDT"
        mock_bot.ex = MagicMock()
        mock_bot.ex.open_orders = AsyncMock(return_value=[])

        def run_in_thread(fn, *args):
            return fn(*args) if args else fn()

        mock_bingx = MagicMock()
        mock_bingx.symbol_info = MagicMock()
        mock_bingx.balance = MagicMock(side_effect=[Decimal("500"), Decimal("100")])
        mock_bingx.price = MagicMock(return_value=Decimal("1.44"))

        with patch.object(mgr, "_get_user_uid", return_value="uid1"):
            with patch.object(mgr, "_get_or_create_bot_for_user", return_value=mock_bot):
                with patch.object(mgr, "_load_api_keys_for_user", return_value=("key", "secret")):
                    with patch.object(mgr.persistence, "load_state", return_value={"symbol": "DOT-USDT"}):
                        with patch("telegram_bot.asyncio.to_thread", new_callable=AsyncMock, side_effect=run_in_thread):
                            with patch("telegram_bot._safe_edit_message", new_callable=AsyncMock) as safe_edit:
                                with patch("telegram_bot.BingXSpot", return_value=mock_bingx):
                                    await mgr.handle_start_bot(query, 100)

        mock_bot.ex.open_orders.assert_called_once()
        msg = safe_edit.call_args[0][1]
        assert "API ключи проверены" in msg or "Баланс" in msg or "Итого" in msg


@pytest.mark.asyncio
class TestHandleStopBotResetProfitBank:
    """Тесты handle_stop_bot: сброс profit_bank и initial_equity при остановке."""

    async def test_handle_stop_bot_resets_profit_bank_and_initial_equity(self):
        """При нажатии Стоп выставляются profit_bank=0 и initial_equity=total_equity, затем save_state."""
        mgr = TelegramBotManager()
        query = AsyncMock()
        query.edit_message_text = AsyncMock()

        mock_bot = MagicMock()
        mock_bot.state = BotState.TRADING
        mock_bot.symbol = "DOT-USDT"
        mock_bot.profit_bank = Decimal("15.50")
        mock_bot.initial_equity = Decimal("600")
        mock_bot.telegram_notifier = AsyncMock()
        mock_bot.ex = MagicMock()
        mock_bot.ex.cancel_all = AsyncMock()
        mock_bot.get_current_price = AsyncMock(return_value=Decimal("1.44"))
        mock_bot.get_total_equity = AsyncMock(return_value=Decimal("1000"))
        mock_bot.save_state = MagicMock()
        mock_bot.statistics = MagicMock()
        mock_bot.statistics.clear_all = MagicMock()

        def run_in_thread(fn, *args):
            return fn(*args) if args else fn()

        with patch.object(mgr, "_get_user_uid", return_value="uid1"):
            with patch.object(mgr, "_get_or_create_bot_for_user", return_value=mock_bot):
                with patch("telegram_bot.asyncio.to_thread", new_callable=AsyncMock, side_effect=run_in_thread):
                    await mgr.handle_stop_bot(query, 100)

        mock_bot.get_current_price.assert_called_once()
        mock_bot.get_total_equity.assert_called_once()
        assert mock_bot.profit_bank == Decimal("0")
        assert mock_bot.initial_equity == Decimal("1000")
        mock_bot.save_state.assert_called_once()
