"""Точка входа: сборка бота (aiogram 3, long polling)."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import settings
from drive_watcher import DriveWatcher, ingest_new_files
from handlers import admin, documents, inn_group, registration
from middlewares import EmployeeMiddleware
from ocr import get_engine
from sheets import SheetsRepo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def _drive_poll_loop(bot: Bot, repo: SheetsRepo, watcher: DriveWatcher) -> None:
    """Каждые settings.drive_poll_seconds проверяет папку на Диске и сама пишет
    найденные служебные записки в Sheets — без карточки подтверждения (доверяем OCR
    полностью, как в потоке ИНН из группы)."""
    notify_chat_id = settings.drive_notify_chat_id or settings.inn_group_id
    logger.info("Слежение за папкой Google Диска включено (каждые %s сек.)", settings.drive_poll_seconds)

    while True:
        try:
            await ingest_new_files(watcher, repo, bot, notify_chat_id, settings.default_currency)
        except Exception:  # noqa: BLE001
            logger.exception("Ошибка при опросе папки Google Диска")
        await asyncio.sleep(settings.drive_poll_seconds)


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
        projects_sheet=settings.sheet_projects,
        log_sheet=settings.sheet_log,
    )
    await repo.init()

    ocr = get_engine(settings)
    logger.info("OCR-движок: %s", ocr.name)

    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher["repo"] = repo
    dispatcher["ocr"] = ocr

    drive_watcher = None
    if settings.drive_folder_id:
        drive_watcher = DriveWatcher(settings.google_credentials_file, settings.drive_folder_id)
    dispatcher["drive_watcher"] = drive_watcher

    employee_middleware = EmployeeMiddleware(repo)
    dispatcher.message.middleware(employee_middleware)
    dispatcher.callback_query.middleware(employee_middleware)

    dispatcher.include_router(registration.router)
    dispatcher.include_router(admin.router)
    dispatcher.include_router(documents.router)
    dispatcher.include_router(inn_group.router)

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен")

    if drive_watcher is not None:
        asyncio.create_task(_drive_poll_loop(bot, repo, drive_watcher))

    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановлено")
