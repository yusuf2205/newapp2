"""Приём документов, распознавание, карточка подтверждения, запись (разделы 2-4 ТЗ)."""

from __future__ import annotations

import logging
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import settings
from keyboards import EDITABLE_FIELDS, confirm_kb, edit_fields_kb, projects_kb, skip_goods_kb
from models import CURRENCIES, NOT_SET, DocumentData, clean_text, format_amount, parse_amount
from ocr import OcrEngine, OcrError
from ocr.docx_engine import DOCX_MIME, recognize_docx
from ocr.xlsx_engine import XLSX_MIME, recognize_xlsx
from sheets import SheetsRepo
from states import DocumentFlow

logger = logging.getLogger(__name__)
router = Router(name="documents")
router.message.filter(F.chat.type == "private")

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


def _goods_step(doc: DocumentData) -> tuple[object, str, object]:
    """Товар уже найден в самом документе (напр. «Предмет договора») — не спрашиваем
    в Телеграме, сразу показываем карточку подтверждения."""
    if doc.goods != NOT_SET:
        return DocumentFlow.confirming, doc.as_card(), confirm_kb()
    return (
        DocumentFlow.asking_goods,
        f"📦 Проект: «{doc.project}».\nУкажите товар/услугу:",
        skip_goods_kb(),
    )


def _all_projects(repo: SheetsRepo) -> list[str]:
    """Список из .env + запомненные через «➕ Другой», без дублей, в порядке добавления."""
    merged = list(settings.projects)
    lowered = {p.lower() for p in merged}
    for name in repo.custom_projects():
        if name.lower() not in lowered:
            merged.append(name)
            lowered.add(name.lower())
    return merged


# --------------------- приём файла ---------------------


@router.message(F.photo | F.document)
async def handle_file(
    message: Message,
    state: FSMContext,
    bot: Bot,
    ocr: OcrEngine,
    employee: dict | None,
    repo: SheetsRepo,
) -> None:
    if not employee:
        await message.answer(NEED_REGISTRATION)
        return

    current = await state.get_state()
    if current in {DocumentFlow.choosing_project.state, DocumentFlow.custom_project.state,
                   DocumentFlow.asking_goods.state, DocumentFlow.confirming.state,
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
        if not (
            mime_type.startswith("image/")
            or mime_type == "application/pdf"
            or mime_type == DOCX_MIME
            or mime_type == XLSX_MIME
        ):
            if mime_type == "application/msword":
                await message.answer(
                    "⚠️ Старый формат .doc не поддерживается — пересохраните как .docx."
                )
            elif mime_type in {
                "application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.template",
            }:
                await message.answer(
                    "⚠️ Старый формат .xls не поддерживается — пересохраните как .xlsx."
                )
            else:
                await message.answer("⚠️ Поддерживаются изображения, PDF, .docx и .xlsx.")
            return
        file_id = document.file_id

    if file_size > MAX_FILE_SIZE:
        await message.answer("⚠️ Файл больше 20 МБ — Telegram не отдаёт такие боту.")
        return

    status = await message.answer("🔎 Распознаю документ…")

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
        logger.warning("OCR failed: %s", exc)
        await status.edit_text(f"❌ Не удалось распознать документ.\n<code>{exc}</code>")
        return
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось обработать файл")
        await status.edit_text("❌ Ошибка при обработке файла. Попробуйте ещё раз.")
        return

    doc = DocumentData.from_ocr(recognized, settings.default_currency)
    if doc.executor == NOT_SET:  # «Ответственный» не найден в документе — берём из Telegram
        doc.executor = employee["fio"]

    identity = f"{employee['fio']} ({employee['position']})"
    doc_number = clean_text(recognized.get("doc_number"), "")
    doc.note = f"{doc_number} — {identity}" if doc_number else identity

    await _save_doc(state, doc)
    await state.update_data(media_group_id=message.media_group_id)

    if doc.project != NOT_SET:
        # Проект уже распознан из документа («№ Заказа/Проект») — ручной выбор не нужен.
        state_, text, kb = _goods_step(doc)
        await state.set_state(state_)
        await status.edit_text(text, reply_markup=kb)
    else:
        await state.set_state(DocumentFlow.choosing_project)
        await status.edit_text(
            "🏗 Выберите проект (объект):", reply_markup=projects_kb(_all_projects(repo))
        )


# --------------------- выбор проекта ---------------------


@router.callback_query(DocumentFlow.choosing_project, F.data.startswith("proj:"))
async def choose_project(call: CallbackQuery, state: FSMContext, repo: SheetsRepo) -> None:
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

    doc.project = _all_projects(repo)[int(value)]
    await _save_doc(state, doc)
    state_, text, kb = _goods_step(doc)
    await state.set_state(state_)
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@router.message(DocumentFlow.custom_project, F.text)
async def custom_project(message: Message, state: FSMContext, repo: SheetsRepo) -> None:
    doc = await _load_doc(state)
    if not doc:
        await state.clear()
        await message.answer("Данные устарели, отправьте документ заново.")
        return

    doc.project = clean_text(message.text)
    if doc.project != NOT_SET:
        await repo.add_project(doc.project)
    await _save_doc(state, doc)
    state_, text, kb = _goods_step(doc)
    await state.set_state(state_)
    await message.answer(text, reply_markup=kb)


# --------------------- товар/услуга ---------------------


@router.message(DocumentFlow.asking_goods, F.text)
async def set_goods(message: Message, state: FSMContext) -> None:
    doc = await _load_doc(state)
    if not doc:
        await state.clear()
        await message.answer("Данные устарели, отправьте документ заново.")
        return

    doc.goods = clean_text(message.text)
    await _save_doc(state, doc)
    await state.set_state(DocumentFlow.confirming)
    await message.answer(doc.as_card(), reply_markup=confirm_kb())


@router.callback_query(DocumentFlow.asking_goods, F.data == "goods:skip")
async def skip_goods(call: CallbackQuery, state: FSMContext) -> None:
    doc = await _load_doc(state)
    if not doc:
        await state.clear()
        await call.answer("Данные устарели, отправьте документ заново", show_alert=True)
        return

    doc.goods = NOT_SET
    await _save_doc(state, doc)
    await state.set_state(DocumentFlow.confirming)
    await _show_card(call.message, doc)
    await call.answer()


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

    await repo.log_action(
        f"{call.from_user.id}",
        "Обновил договор" if updated else "Записал документ",
        f"строка {row}, {doc.contract}, {doc.counterparty}, "
        f"{format_amount(doc.paid)} {doc.currency}",
    )

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
async def apply_edit(message: Message, state: FSMContext, repo: SheetsRepo) -> None:
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
        if key == "project" and doc.project != NOT_SET:
            await repo.add_project(doc.project)

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
