"""Группа для выгрузки договоров: извлекаем полные данные и сверяем с тем, что уже
накопилось в Google Sheets из служебных записок (см. drive_watcher.py).

Без карточки подтверждения и без FSM — сразу распознаём и отвечаем результатом.
Не привязано к регистрации сотрудников: реагирует на любого, кто прислал файл в
группе (это отдельный, более простой поток, чем личный чат с ботом).
"""

from __future__ import annotations

import logging
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.types import Message

from config import settings
from drive_watcher import DriveWatcher, ingest_new_files
from models import NOT_SET, DocumentData, format_amount
from ocr import OcrEngine, OcrError
from ocr.docx_engine import DOCX_MIME, recognize_docx
from ocr.xlsx_engine import XLSX_MIME, recognize_xlsx
from sheets import SheetsRepo

logger = logging.getLogger(__name__)
router = Router(name="inn_group")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

MAX_FILE_SIZE = 20 * 1024 * 1024


@router.message(F.photo | F.document)
async def handle_group_file(
    message: Message,
    bot: Bot,
    ocr: OcrEngine,
    repo: SheetsRepo,
    drive_watcher: DriveWatcher | None = None,
) -> None:
    logger.info("Файл в группе «%s» (chat.id=%s)", message.chat.title, message.chat.id)

    if settings.inn_group_id and message.chat.id != settings.inn_group_id:
        logger.info(
            "Файл из непривязанной группы chat.id=%s (%s) — игнорирую. "
            "Впишите этот id в INN_GROUP_CHAT_ID, если это нужная группа.",
            message.chat.id, message.chat.title,
        )
        return  # бот добавлен в другую группу — не мешаем

    if message.photo:
        file_id = message.photo[-1].file_id
        mime_type = "image/jpeg"
        file_size = message.photo[-1].file_size or 0
    else:
        document = message.document
        mime_type = (document.mime_type or "").lower()
        file_size = document.file_size or 0
        if not (
            mime_type.startswith("image/")
            or mime_type == "application/pdf"
            or mime_type == DOCX_MIME
            or mime_type == XLSX_MIME
        ):
            if mime_type == "application/msword":
                await message.reply("⚠️ Старый формат .doc не поддерживается — пересохраните как .docx.")
            elif mime_type in {
                "application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.template",
            }:
                await message.reply("⚠️ Старый формат .xls не поддерживается — пересохраните как .xlsx.")
            # На остальное (не документ вовсе — стикеры, голосовые и т.п.) молчим:
            # это не похоже на договор, и предупреждение было бы просто шумом.
            return
        file_id = document.file_id

    if file_size > MAX_FILE_SIZE:
        return

    try:
        buffer = BytesIO()
        await bot.download(file_id, destination=buffer)
        content = buffer.getvalue()
        if mime_type == DOCX_MIME:
            recognized = await recognize_docx(content)
        elif mime_type == XLSX_MIME:
            recognized = await recognize_xlsx(content)
        else:
            recognized = await ocr.recognize(content, mime_type)
    except OcrError as exc:
        logger.warning("OCR failed in group: %s", exc)
        await message.reply(f"❌ Не удалось распознать документ.\n<code>{exc}</code>")
        return
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось обработать файл в группе")
        await message.reply("❌ Ошибка при обработке файла.")
        return

    doc = DocumentData.from_ocr(recognized, settings.default_currency)

    if doc.contract == NOT_SET and doc.counterparty == NOT_SET and doc.inn == NOT_SET:
        await message.reply("⚠️ Не нашёл ни договора, ни контрагента, ни ИНН в этом документе.")
        return

    # Сначала подхватываем свежие служебные записки с Диска — вдруг они добавлены
    # только что и ещё не подобраны периодическим опросом (см. main.py). Так «Оплачено»
    # в сверке ниже будет максимально актуальным. notify_chat_id=0 — не шлём отдельные
    # сообщения по каждой записке, чтобы не спамить прямо перед итоговой сверкой.
    if drive_watcher is not None and settings.drive_folder_id:
        try:
            await ingest_new_files(drive_watcher, repo, bot, 0, settings.default_currency)
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось обновить служебные записки с Диска перед сверкой договора")

    try:
        result = await repo.upsert_contract(doc)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось записать договор")
        await message.reply("❌ Ошибка записи в Google Sheets. Попробуйте позже.")
        return

    lines = [
        f"♻️ Договор сверен, строка №{result['row']}."
        if result["updated"]
        else f"✅ Договор записан, строка №{result['row']}."
    ]
    if doc.contract != NOT_SET:
        lines.append(f"Договор: {doc.contract}")
    if result["counterparty"] != NOT_SET:
        lines.append(f"Поставщик: {result['counterparty']}")
    if doc.inn != NOT_SET:
        lines.append(f"ИНН: {doc.inn}")
    if result["total"] is not None:
        lines.append(f"Сумма договора: {format_amount(result['total'])} {doc.currency}")
    lines.append(f"Оплачено (по служебным запискам): {format_amount(result['paid'])} {doc.currency}")
    if result["balance"] is not None:
        lines.append(f"Остаток: {format_amount(result['balance'])} {doc.currency}")
        if result["balance"] < 0:
            lines.append("⚠️ Оплачено больше суммы договора — проверьте вручную!")

    actor = f"{message.from_user.id}" + (f" (@{message.from_user.username})" if message.from_user.username else "")
    await repo.log_action(
        actor,
        "Сверил договор из группы" if result["updated"] else "Записал договор из группы",
        f"строка {result['row']}, {doc.contract}, {result['counterparty']}",
    )

    await message.reply("\n".join(lines))
