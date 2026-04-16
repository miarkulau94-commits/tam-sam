"""
Tests for main.py — BotRunner, main() config validation.
"""

import asyncio
import os
import sys
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestBotRunner:
    """BotRunner initialization and config."""

    def test_init(self):
        with patch("main.TelegramBotManager"):
            from main import BotRunner

            runner = BotRunner()
            assert runner.restart_count == 0
            assert runner.max_restarts == 10
            assert runner.restart_delay == 60
            assert runner.telegram_bot is not None

    @pytest.mark.asyncio
    async def test_run_telegram_bot_invalid_token_exits(self):
        from main import BotRunner, InvalidToken

        with patch("main.TelegramBotManager") as MockMgr:
            mock_tg = MagicMock()
            app = MagicMock()
            app.start = AsyncMock(side_effect=InvalidToken("bad token"))
            app.__aenter__ = AsyncMock(return_value=app)
            app.__aexit__ = AsyncMock(return_value=None)
            mock_tg.initialize.return_value = app
            MockMgr.return_value = mock_tg

            runner = BotRunner()
            with patch("main.sys.exit") as mock_exit:
                await runner.run_telegram_bot()
                mock_exit.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_run_with_restart_stops_on_keyboard_interrupt(self):
        from main import BotRunner

        with patch("main.TelegramBotManager") as MockMgr:
            mock_app = AsyncMock()
            mock_app.start = AsyncMock(side_effect=KeyboardInterrupt())
            mock_app.stop = AsyncMock()
            mock_app.updater = MagicMock()
            mock_app.updater.start_polling = AsyncMock()
            mock_app.updater.stop = AsyncMock()
            mock_app.__aenter__ = AsyncMock(return_value=mock_app)
            mock_app.__aexit__ = AsyncMock(return_value=None)
            MockMgr.return_value.initialize.return_value = mock_app

            runner = BotRunner()
            await runner.run_with_restart()
            assert runner.restart_count == 0


class TestMainConfig:
    """main() configuration checks — patch config before calling main()."""

    def test_main_exits_if_no_tg_token(self):
        with patch("main.config") as mock_config:
            mock_config.TG_TOKEN = ""
            with patch("main.sys.exit") as mock_exit:
                with patch("main.BotRunner"):
                    from main import main
                    main()
                    mock_exit.assert_called_with(1)

    def test_main_exits_if_token_placeholder(self):
        with patch("main.config") as mock_config:
            mock_config.TG_TOKEN = "your_bot_token_here"
            mock_config.API_KEY = None
            mock_config.SECRET = None
            mock_config.ENCRYPTION_SECRET = ""
            with patch("main.sys.exit") as mock_exit:
                with patch("main.BotRunner"):
                    from main import main
                    main()
                    mock_exit.assert_called_with(1)

    def test_main_exits_if_no_encryption_secret(self):
        with patch("main.config") as mock_config:
            mock_config.TG_TOKEN = "valid_token_123"
            mock_config.API_KEY = None
            mock_config.SECRET = None
            mock_config.ENCRYPTION_SECRET = ""
            with patch("main.sys.exit") as mock_exit:
                with patch("main.BotRunner"):
                    from main import main
                    main()
                    mock_exit.assert_called_with(1)

    def test_main_runs_runner_when_config_ok(self):
        with patch("main.config") as mock_config:
            mock_config.TG_TOKEN = "valid_token"
            mock_config.API_KEY = None
            mock_config.SECRET = None
            mock_config.ENCRYPTION_SECRET = "enc_secret"
            mock_runner = MagicMock()
            with patch("main.BotRunner", return_value=mock_runner):
                from main import main
                main()
                mock_runner.run.assert_called_once()

    def test_main_warns_when_api_keys_missing_but_encryption_ok(self):
        with patch("main.config") as mock_config:
            mock_config.TG_TOKEN = "valid_token_abc_unique"
            mock_config.API_KEY = None
            mock_config.SECRET = None
            mock_config.ENCRYPTION_SECRET = "enc_secret"
            mock_runner = MagicMock()
            with patch("main.BotRunner", return_value=mock_runner):
                with patch("main.log") as mock_log:
                    from main import main

                    main()
                    mock_runner.run.assert_called_once()
                    texts = [str(c[0][0]) for c in mock_log.warning.call_args_list if c[0]]
                    assert any("API keys not set" in t for t in texts)


