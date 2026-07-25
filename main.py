"""Точка входа: сборка бота (aiogram 3, long polling)."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import settings
from handlers import documents, registration
from middlewares import EmployeeMiddleware
from ocr import get_engine
from sheets import SheetsRepo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    settings.validate()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    repo = SheetsRepo(
        credentials_file=settings.google_credentials_file,
        spreadsheet_id=settings.spreadsheet_id,
        data_sheet=settings.sheet_data,
        employees_sheet=settings.sheet_employees,
    )
    await repo.init()

    ocr = get_engine(settings)
    logger.info("OCR-движок: %s", ocr.name)

    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher["repo"] = repo
    dispatcher["ocr"] = ocr

    employee_middleware = EmployeeMiddleware(repo)
    dispatcher.message.middleware(employee_middleware)
    dispatcher.callback_query.middleware(employee_middleware)

    dispatcher.include_router(registration.router)
    dispatcher.include_router(documents.router)

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен")
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановлено")
