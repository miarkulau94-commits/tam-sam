"""
AutoScaleX Pro 2.2 - Главный файл запуска
С автоматическим перезапуском при падении
"""

import asyncio
import logging
import os
import sys
from datetime import datetime

import config
from telegram_bot import TelegramBotManager

try:
    from telegram.error import InvalidToken
except ImportError:
    InvalidToken = type("InvalidToken", (Exception,), {})

# Настройка логирования
os.makedirs(config.LOG_DIR, exist_ok=True)
log_file = os.path.join(config.LOG_DIR, f"bot_{datetime.now().strftime('%Y%m%d')}.log")

from logging.handlers import RotatingFileHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

log = logging.getLogger("main")

# Снизить шум в логах: HTTP-запросы и persistence — только при проблемах
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("persistence").setLevel(logging.WARNING)


class BotRunner:
    """Класс для запуска бота с автоматическим перезапуском"""

    def __init__(self):
        self.telegram_bot = TelegramBotManager()
        self.restart_count = 0
        self.max_restarts = 10
        self.restart_delay = 60  # секунд

    async def run_telegram_bot(self):
        """Запустить Telegram бота"""
        try:
            app = self.telegram_bot.initialize()
            log.info("Starting Telegram bot...")

            async with app:
                await app.start()
                await app.updater.start_polling()
                log.info("Telegram bot started successfully")

                # Авто-восстановление ботов, которые были запущены до перезапуска/падения интернета
                await self.telegram_bot.restore_running_bots()

                # Ожидаем завершения
                stop_event = asyncio.Event()
                try:
                    await stop_event.wait()
                except asyncio.CancelledError:
                    pass

                await app.updater.stop()
                await app.stop()
        except InvalidToken:
            log.error(
                "Неверный Telegram токен. Токен отклонён сервером. "
                "Проверьте TG_TOKEN в .env и получите новый токен у @BotFather."
            )
            sys.exit(1)
        except Exception as e:
            log.exception(f"Error in Telegram bot: {e}")
            raise

    async def run_with_restart(self):
        """Запустить бота с автоматическим перезапуском"""
        try:
            while self.restart_count < self.max_restarts:
                try:
                    log.info(f"Starting bot (attempt {self.restart_count + 1}/{self.max_restarts})...")
                    await self.run_telegram_bot()
                except KeyboardInterrupt:
                    log.info("Bot stopped by user")
                    break
                except Exception as e:
                    self.restart_count += 1
                    log.error(f"Bot crashed (attempt {self.restart_count}): {e}")

                    if self.restart_count < self.max_restarts:
                        log.info(f"Restarting in {self.restart_delay} seconds...")
                        await asyncio.sleep(self.restart_delay)
                    else:
                        log.error("Max restart attempts reached. Exiting.")
                        break
        except KeyboardInterrupt:
            log.info("Bot stopped by user (outer)")

    def run(self):
        """Синхронный запуск"""
        try:
            asyncio.run(self.run_with_restart())
        except KeyboardInterrupt:
            log.info("Bot stopped")


def main():
    """Главная функция"""
    log.info("=" * 50)
    log.info("AutoScaleX Pro 2.2 Starting...")
    log.info("=" * 50)

    # Проверка конфигурации
    if not config.TG_TOKEN:
        log.error("TG_TOKEN not set in environment variables!")
        sys.exit(1)

    if "your_bot_token" in (config.TG_TOKEN or "").lower() or "botfather" in (config.TG_TOKEN or "").lower():
        log.error("TG_TOKEN содержит плейсхолдер! Замените на реальный токен от @BotFather.")
        sys.exit(1)

    if not config.API_KEY and not config.SECRET:
        log.warning("API keys not set. Users will need to set them via /set_api")

    # ENCRYPTION_SECRET обязателен
    if not getattr(config, "ENCRYPTION_SECRET", ""):
        log.error("ENCRYPTION_SECRET обязателен! Задайте в .env.")
        sys.exit(1)

    runner = BotRunner()
    runner.run()


if __name__ == "__main__":
    main()
