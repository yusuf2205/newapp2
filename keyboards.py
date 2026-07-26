"""Инлайн-клавиатуры."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Поля, доступные для ручного редактирования: callback -> (подпись, атрибут)
EDITABLE_FIELDS = {
    "counterparty": ("Контрагент", "counterparty"),
    "contract": ("Договор", "contract"),
    "contract_type": ("Тип контракта", "contract_type"),
    "goods": ("Товар", "goods"),
    "total": ("Общая сумма", "total"),
    "paid": ("Оплачено", "paid"),
    "currency": ("Валюта", "currency"),
    "note": ("Примечание", "note"),
    "project": ("Проект", "project"),
    "inn": ("ИНН", "inn"),
}


def projects_kb(projects: list[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, title in enumerate(projects):
        builder.button(text=title, callback_data=f"proj:{index}")
    builder.button(text="➕ Другой", callback_data="proj:other")
    builder.adjust(2)
    return builder.as_markup()


def skip_goods_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⏭ Пропустить", callback_data="goods:skip")]]
    )


def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить и записать", callback_data="doc:confirm")],
            [
                InlineKeyboardButton(text="✏️ Редактировать", callback_data="doc:edit"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="doc:cancel"),
            ],
        ]
    )


def approve_registration_kb(telegram_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Разрешить", callback_data=f"reg:approve:{telegram_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reg:deny:{telegram_id}"),
        ]]
    )


def employee_status_kb(telegram_id: int, status: str) -> InlineKeyboardMarkup:
    """Кнопки зависят от текущего статуса — не показываем действие, которое и так уже применено."""
    buttons: list[InlineKeyboardButton] = []
    if status != "Активен":
        buttons.append(InlineKeyboardButton(text="✅ Разблокировать", callback_data=f"emp:activate:{telegram_id}"))
    if status != "Заблокирован":
        buttons.append(InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"emp:block:{telegram_id}"))
    if status != "В архиве":
        buttons.append(InlineKeyboardButton(text="📦 В архив", callback_data=f"emp:archive:{telegram_id}"))
    builder = InlineKeyboardBuilder()
    for button in buttons:
        builder.add(button)
    builder.adjust(1)
    return builder.as_markup()


def edit_fields_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, (label, _) in EDITABLE_FIELDS.items():
        builder.button(text=label, callback_data=f"edit:{key}")
    builder.button(text="⬅️ Назад к карточке", callback_data="edit:back")
    builder.adjust(2)
    return builder.as_markup()
