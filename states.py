"""Состояния FSM."""

from aiogram.fsm.state import State, StatesGroup


class Registration(StatesGroup):
    fio = State()
    position = State()


class DocumentFlow(StatesGroup):
    choosing_project = State()
    custom_project = State()
    asking_goods = State()
    confirming = State()
    editing_value = State()
