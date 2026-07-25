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
}


def projects_kb(projects: list[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, title in enumerate(projects):
        builder.button(text=title, callback_data=f"proj:{index}")
    builder.button(text="➕ Другой", callback_data="proj:other")
    builder.adjust(2)
    return builder.as_markup()


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


def edit_fields_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, (label, _) in EDITABLE_FIELDS.items():
        builder.button(text=label, callback_data=f"edit:{key}")
    builder.button(text="⬅️ Назад к карточке", callback_data="edit:back")
    builder.adjust(2)
    return builder.as_markup()
