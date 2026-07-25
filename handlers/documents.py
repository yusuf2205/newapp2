"""Приём документов, распознавание, карточка подтверждения, запись (разделы 2-4 ТЗ)."""

from __future__ import annotations

import logging
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import settings
from keyboards import EDITABLE_FIELDS, confirm_kb, edit_fields_kb, projects_kb
from models import CURRENCIES, NOT_SET, DocumentData, clean_text, format_amount, parse_amount
from ocr import OcrEngine, OcrError
from sheets import SheetsRepo
from states import DocumentFlow

logger = logging.getLogger(__name__)
router = Router(name="documents")

MAX_FILE_SIZE = 20 * 1024 * 1024  # лимит скачивания файлов Bot API
NEED_REGISTRATION = "🔒 Сначала пройдите регистрацию — отправьте /start."


async def _load_doc(state: FSMContext) -> DocumentData | None:
    data = await state.get_data()
    payload = data.get("doc")
    return DocumentData.from_state(payload) if payload else None


async def _save_doc(state: FSMContext, doc: DocumentData) -> None:
    await state.update_data(doc=doc.to_state())


async def _show_card(message: Message, doc: DocumentData) -> None:
    await message.edit_text(doc.as_card(), reply_markup=confirm_kb())


# --------------------- приём файла ---------------------


@router.message(F.photo | F.document)
async def handle_file(
    message: Message,
    state: FSMContext,
    bot: Bot,
    ocr: OcrEngine,
    employee: dict | None,
) -> None:
    if not employee:
        await message.answer(NEED_REGISTRATION)
        return

    current = await state.get_state()
    if current in {DocumentFlow.choosing_project.state, DocumentFlow.confirming.state,
                   DocumentFlow.editing_value.state}:
        data = await state.get_data()
        group = message.media_group_id
        if group and data.get("media_group_id") == group:
            return  # остальные фото альбома молча игнорируем
        await message.answer(
            "⏳ Сначала завершите работу с предыдущим документом "
            "(«Подтвердить» или «Отмена»), затем присылайте следующий."
        )
        return

    if message.photo:
        file_id = message.photo[-1].file_id
        mime_type = "image/jpeg"
        file_size = message.photo[-1].file_size or 0
    else:
        document = message.document
        mime_type = (document.mime_type or "").lower()
        file_size = document.file_size or 0
        if not (mime_type.startswith("image/") or mime_type == "application/pdf"):
            await message.answer("⚠️ Поддерживаются только изображения и PDF-файлы.")
            return
        file_id = document.file_id

    if file_size > MAX_FILE_SIZE:
        await message.answer("⚠️ Файл больше 20 МБ — Telegram не отдаёт такие боту.")
        return

    status = await message.answer("🔎 Распознаю документ…")

    try:
        buffer = BytesIO()
        await bot.download(file_id, destination=buffer)
        recognized = await ocr.recognize(buffer.getvalue(), mime_type)
    except OcrError as exc:
        logger.warning("OCR failed: %s", exc)
        await status.edit_text(f"❌ Не удалось распознать документ.\n<code>{exc}</code>")
        return
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось обработать файл")
        await status.edit_text("❌ Ошибка при обработке файла. Попробуйте ещё раз.")
        return

    doc = DocumentData.from_ocr(recognized, settings.default_currency)
    doc.executor = employee["fio"]
    doc.note = employee["position"]

    await _save_doc(state, doc)
    await state.update_data(media_group_id=message.media_group_id)
    await state.set_state(DocumentFlow.choosing_project)
    await status.edit_text(
        "🏗 Выберите проект (объект):", reply_markup=projects_kb(settings.projects)
    )


# --------------------- выбор проекта ---------------------


@router.callback_query(DocumentFlow.choosing_project, F.data.startswith("proj:"))
async def choose_project(call: CallbackQuery, state: FSMContext) -> None:
    value = call.data.split(":", 1)[1]

    if value == "other":
        await state.set_state(DocumentFlow.custom_project)
        await call.message.edit_text("✏️ Введите название проекта текстом:")
        await call.answer()
        return

    doc = await _load_doc(state)
    if not doc:
        await call.answer("Данные устарели, отправьте документ заново", show_alert=True)
        await state.clear()
        return

    doc.project = settings.projects[int(value)]
    await _save_doc(state, doc)
    await state.set_state(DocumentFlow.confirming)
    await _show_card(call.message, doc)
    await call.answer()


@router.message(DocumentFlow.custom_project, F.text)
async def custom_project(message: Message, state: FSMContext) -> None:
    doc = await _load_doc(state)
    if not doc:
        await state.clear()
        await message.answer("Данные устарели, отправьте документ заново.")
        return

    doc.project = clean_text(message.text)
    await _save_doc(state, doc)
    await state.set_state(DocumentFlow.confirming)
    await message.answer(doc.as_card(), reply_markup=confirm_kb())


# --------------------- подтверждение / отмена ---------------------


@router.callback_query(DocumentFlow.confirming, F.data == "doc:confirm")
async def confirm(call: CallbackQuery, state: FSMContext, repo: SheetsRepo) -> None:
    doc = await _load_doc(state)
    if not doc:
        await call.answer("Данные устарели, отправьте документ заново", show_alert=True)
        await state.clear()
        return

    if doc.contract == NOT_SET and doc.counterparty == NOT_SET:
        await call.answer(
            "Заполните хотя бы контрагента или номер договора", show_alert=True
        )
        return

    await call.message.edit_text("💾 Записываю в таблицу…")
    try:
        row, updated = await repo.upsert_document(doc)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось записать строку")
        await call.message.edit_text("❌ Ошибка записи в Google Sheets. Попробуйте позже.")
        await call.answer()
        return

    await state.clear()
    if updated:
        text = (
            f"♻️ Договор уже был в таблице — обновил строку <b>№{row}</b>.\n"
            f"Добавлена оплата: {format_amount(doc.paid)} {doc.currency}."
        )
    else:
        text = f"✅ Записано в таблицу, строка <b>№{row}</b>."

    await call.message.edit_text(f"{doc.as_card()}\n\n{text}")
    await call.answer("Готово")


@router.callback_query(F.data == "doc:cancel")
async def cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("❌ Отменено. Документ не записан.")
    await call.answer()


# --------------------- редактирование ---------------------


@router.callback_query(DocumentFlow.confirming, F.data == "doc:edit")
async def edit_menu(call: CallbackQuery) -> None:
    await call.message.edit_text(
        "✏️ Что исправить?", reply_markup=edit_fields_kb()
    )
    await call.answer()


@router.callback_query(DocumentFlow.confirming, F.data == "edit:back")
async def edit_back(call: CallbackQuery, state: FSMContext) -> None:
    doc = await _load_doc(state)
    if not doc:
        await state.clear()
        await call.answer("Данные устарели", show_alert=True)
        return
    await _show_card(call.message, doc)
    await call.answer()


@router.callback_query(DocumentFlow.confirming, F.data.startswith("edit:"))
async def edit_field(call: CallbackQuery, state: FSMContext) -> None:
    key = call.data.split(":", 1)[1]
    if key not in EDITABLE_FIELDS:
        await call.answer()
        return

    label, _ = EDITABLE_FIELDS[key]
    hints = {
        "total": "числом, например 177800000",
        "paid": "числом, например 50000000",
        "currency": "одно из: UZS, USD, EUR, RUB",
        "contract_type": "Местный или Импорт",
    }
    hint = f" ({hints[key]})" if key in hints else ""

    await state.update_data(edit_field=key)
    await state.set_state(DocumentFlow.editing_value)
    await call.message.edit_text(f"Введите новое значение — <b>{label}</b>{hint}:")
    await call.answer()


@router.message(DocumentFlow.editing_value, F.text)
async def apply_edit(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    key = data.get("edit_field")
    doc = await _load_doc(state)

    if not doc or key not in EDITABLE_FIELDS:
        await state.clear()
        await message.answer("Данные устарели, отправьте документ заново.")
        return

    value = " ".join(message.text.split())

    if key in {"total", "paid"}:
        amount = parse_amount(value)
        if amount is None:
            await message.answer("Не похоже на число. Введите сумму ещё раз:")
            return
        setattr(doc, key, amount)
    elif key == "currency":
        code = value.upper()
        if code not in CURRENCIES:
            await message.answer("Допустимо: UZS, USD, EUR, RUB. Введите ещё раз:")
            return
        doc.currency = code
    elif key == "contract_type":
        if value.lower() not in {"местный", "импорт"}:
            await message.answer("Допустимо: Местный или Импорт. Введите ещё раз:")
            return
        doc.contract_type = value.capitalize()
    else:
        setattr(doc, key, clean_text(value))

    await _save_doc(state, doc)
    await state.set_state(DocumentFlow.confirming)
    await message.answer(doc.as_card(), reply_markup=confirm_kb())


# --------------------- прочее ---------------------


@router.message(F.text, ~F.text.startswith("/"))
async def fallback(message: Message, employee: dict | None) -> None:
    if not employee:
        await message.answer(NEED_REGISTRATION)
        return
    await message.answer(
        "📎 Пришлите фото или PDF документа — чек, счёт или договор.\n"
        "Команды: /start — начать заново, /cancel — сбросить текущую операцию."
    )
