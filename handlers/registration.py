"""Регистрация сотрудника (раздел 1 ТЗ) + подтверждение доступа админом.

Если ADMIN_IDS настроен, новая заявка не активируется сразу — статус «Ожидает
подтверждения», админу(ам) уходит сообщение с кнопками «Разрешить»/«Отклонить».
Если ADMIN_IDS пуст, поведение как раньше — регистрация сразу активна (чтобы
не заблокировать бота для тех, кто не настраивал админов).
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import settings
from keyboards import approve_registration_kb
from middlewares import EmployeeMiddleware
from models import STATUS_ACTIVE, STATUS_DENIED, STATUS_PENDING
from sheets import SheetsRepo
from states import Registration

router = Router(name="registration")
router.message.filter(F.chat.type == "private")

WELCOME_BACK = (
    "👋 С возвращением, <b>{fio}</b> ({position})!\n\n"
    "Отправьте фото или PDF чека, счёта либо договора — я распознаю данные "
    "и внесу их в таблицу."
)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, employee: dict | None) -> None:
    await state.clear()

    if employee:
        await message.answer(
            WELCOME_BACK.format(fio=employee["fio"], position=employee["position"])
        )
        return

    await state.set_state(Registration.fio)
    await message.answer(
        "👤 Вы у меня впервые. Давайте зарегистрируемся.\n\n"
        "Введите ваше <b>ФИО</b> (например: Иванов Иван):"
    )


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено. Отправьте документ или /start.")


@router.message(Registration.fio, F.text)
async def set_fio(message: Message, state: FSMContext) -> None:
    fio = " ".join(message.text.split())
    if len(fio) < 3:
        await message.answer("Слишком короткое ФИО. Введите ещё раз:")
        return

    await state.update_data(fio=fio)
    await state.set_state(Registration.position)
    await message.answer(
        "💼 Укажите вашу <b>должность</b> (например: Прораб, Закупщик, Бухгалтер):"
    )


@router.message(Registration.position, F.text)
async def set_position(
    message: Message,
    state: FSMContext,
    repo: SheetsRepo,
    employees_cache: EmployeeMiddleware,
    bot: Bot,
) -> None:
    position = " ".join(message.text.split())
    if len(position) < 2:
        await message.answer("Слишком короткое название должности. Введите ещё раз:")
        return

    data = await state.get_data()
    fio = data["fio"]
    telegram_id = message.from_user.id
    await state.clear()

    if not settings.admin_ids:
        # Админы не настроены — не блокируем бота никому, регистрируем сразу.
        await repo.add_employee(telegram_id, fio, position, status=STATUS_ACTIVE)
        employees_cache.invalidate(telegram_id)
        await message.answer(
            "✅ <b>Регистрация завершена!</b>\n"
            f"ФИО: {fio}\nДолжность: {position}\n\n"
            "Теперь вы можете отправлять фото чеков, счетов и договоров."
        )
        return

    await repo.add_employee(telegram_id, fio, position, status=STATUS_PENDING)
    await message.answer(
        "⏳ Заявка отправлена администратору на подтверждение.\n"
        f"ФИО: {fio}\nДолжность: {position}\n\n"
        "Как только доступ одобрят, я вам напишу."
    )

    admin_text = (
        f"🆕 <b>Новая заявка на регистрацию</b>\n"
        f"ФИО: {fio}\nДолжность: {position}\nTelegram ID: <code>{telegram_id}</code>"
        + (f"\nЮзернейм: @{message.from_user.username}" if message.from_user.username else "")
    )
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, admin_text, reply_markup=approve_registration_kb(telegram_id))
        except Exception:  # noqa: BLE001
            pass  # админ мог не запускать бота лично — не роняем регистрацию из-за этого


@router.message(Registration.fio)
@router.message(Registration.position)
async def registration_wrong_type(message: Message) -> None:
    await message.answer("Пожалуйста, отправьте ответ текстом.")


# --------------------- решение админа по заявке ---------------------


@router.callback_query(F.data.startswith("reg:"))
async def handle_registration_decision(
    call: CallbackQuery, repo: SheetsRepo, employees_cache: EmployeeMiddleware, bot: Bot
) -> None:
    if call.from_user.id not in settings.admin_ids:
        await call.answer("Только администратор может это сделать", show_alert=True)
        return

    _, action, raw_id = call.data.split(":", 2)
    telegram_id = int(raw_id)

    if action == "approve":
        employee = await repo.set_employee_status(telegram_id, STATUS_ACTIVE)
        employees_cache.invalidate(telegram_id)
        if employee:
            await repo.log_action(
                str(call.from_user.id), "Одобрил регистрацию",
                f"{employee['fio']} ({telegram_id})",
            )
            try:
                await bot.send_message(
                    telegram_id,
                    "✅ Ваш доступ подтверждён администратором! Можете отправлять документы.",
                )
            except Exception:  # noqa: BLE001
                pass
            await call.message.edit_text(call.message.text + "\n\n✅ Одобрено")
        else:
            await call.message.edit_text(call.message.text + "\n\n⚠️ Заявка не найдена (уже обработана?)")
    elif action == "deny":
        employee = await repo.set_employee_status(telegram_id, STATUS_DENIED)
        employees_cache.invalidate(telegram_id)
        if employee:
            await repo.log_action(
                str(call.from_user.id), "Отклонил регистрацию",
                f"{employee['fio']} ({telegram_id})",
            )
            try:
                await bot.send_message(telegram_id, "❌ Администратор отклонил вашу заявку на доступ.")
            except Exception:  # noqa: BLE001
                pass
            await call.message.edit_text(call.message.text + "\n\n❌ Отклонено")
        else:
            await call.message.edit_text(call.message.text + "\n\n⚠️ Заявка не найдена (уже обработана?)")

    await call.answer()
