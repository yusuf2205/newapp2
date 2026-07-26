"""Панель администратора: список сотрудников с возможностью блокировать/архивировать/
разблокировать доступ. Ничего не делает для не-админов (проверка по ADMIN_IDS)."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from config import settings
from keyboards import employee_status_kb
from middlewares import EmployeeMiddleware
from models import STATUS_ACTIVE, STATUS_ARCHIVED, STATUS_BLOCKED
from sheets import SheetsRepo

router = Router(name="admin")
router.message.filter(F.chat.type == "private")

NEED_ADMIN = "🔒 Эта команда доступна только администратору."

_ACTION_LABELS = {
    "activate": ("Разблокировал", STATUS_ACTIVE, "✅ Ваш доступ восстановлен администратором."),
    "block": ("Заблокировал", STATUS_BLOCKED, "🚫 Ваш доступ заблокирован администратором."),
    "archive": ("Отправил в архив", STATUS_ARCHIVED, "📦 Ваш доступ переведён в архив администратором."),
}


def _employee_card(employee: dict) -> str:
    return (
        f"👤 <b>{employee['fio']}</b> ({employee['position']})\n"
        f"Telegram ID: <code>{employee['telegram_id']}</code>\n"
        f"Статус: {employee['status']}\n"
        f"Зарегистрирован: {employee['date']}"
    )


@router.message(Command("employees"))
async def list_employees(message: Message, repo: SheetsRepo) -> None:
    if message.from_user.id not in settings.admin_ids:
        await message.answer(NEED_ADMIN)
        return

    employees = await repo.list_employees()
    if not employees:
        await message.answer("Список сотрудников пуст.")
        return

    for employee in employees:
        await message.answer(
            _employee_card(employee),
            reply_markup=employee_status_kb(int(employee["telegram_id"]), employee["status"]),
        )


@router.callback_query(F.data.startswith("emp:"))
async def change_employee_status(
    call: CallbackQuery, repo: SheetsRepo, employees_cache: EmployeeMiddleware, bot: Bot
) -> None:
    if call.from_user.id not in settings.admin_ids:
        await call.answer("Только администратор может это сделать", show_alert=True)
        return

    _, action, raw_id = call.data.split(":", 2)
    if action not in _ACTION_LABELS:
        await call.answer()
        return

    telegram_id = int(raw_id)
    log_verb, new_status, notify_text = _ACTION_LABELS[action]

    employee = await repo.set_employee_status(telegram_id, new_status)
    if not employee:
        await call.answer("Сотрудник не найден", show_alert=True)
        return

    employees_cache.invalidate(telegram_id)
    await repo.log_action(str(call.from_user.id), log_verb, f"{employee['fio']} ({telegram_id})")

    try:
        await bot.send_message(telegram_id, notify_text)
    except Exception:  # noqa: BLE001
        pass  # сотрудник мог заблокировать бота — не критично

    await call.message.edit_text(
        _employee_card(employee), reply_markup=employee_status_kb(telegram_id, new_status)
    )
    await call.answer("Готово")
