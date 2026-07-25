"""Регистрация сотрудника (раздел 1 ТЗ)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from middlewares import EmployeeMiddleware
from sheets import SheetsRepo
from states import Registration

router = Router(name="registration")

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
) -> None:
    position = " ".join(message.text.split())
    if len(position) < 2:
        await message.answer("Слишком короткое название должности. Введите ещё раз:")
        return

    data = await state.get_data()
    await repo.add_employee(message.from_user.id, data["fio"], position)
    employees_cache.invalidate(message.from_user.id)
    await state.clear()

    await message.answer(
        "✅ <b>Регистрация завершена!</b>\n"
        f"ФИО: {data['fio']}\nДолжность: {position}\n\n"
        "Теперь вы можете отправлять фото чеков, счетов и договоров."
    )


@router.message(Registration.fio)
@router.message(Registration.position)
async def registration_wrong_type(message: Message) -> None:
    await message.answer("Пожалуйста, отправьте ответ текстом.")
