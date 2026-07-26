"""Мидлварь: подтягивает карточку сотрудника из Google Sheets и кэширует её."""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from sheets import SheetsRepo

CACHE_TTL = 600  # секунд


class EmployeeMiddleware(BaseMiddleware):
    def __init__(self, repo: SheetsRepo) -> None:
        self._repo = repo
        self._cache: dict[int, tuple[float, dict | None]] = {}

    def invalidate(self, telegram_id: int) -> None:
        self._cache.pop(telegram_id, None)

    async def _get(self, telegram_id: int) -> dict | None:
        cached = self._cache.get(telegram_id)
        if cached and time.monotonic() - cached[0] < CACHE_TTL:
            return cached[1]

        employee = await self._repo.get_employee(telegram_id)
        if employee and employee.get("status", "").lower().startswith("актив") is False:
            employee = None  # уволенные/заблокированные не работают с ботом
        self._cache[telegram_id] = (time.monotonic(), employee)
        return employee

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat = getattr(event, "chat", None) or getattr(getattr(event, "message", None), "chat", None)
        is_private = chat is None or chat.type == "private"

        user = data.get("event_from_user")
        # В группе (поток извлечения ИНН) карточка сотрудника не нужна — не дёргаем
        # Sheets лишний раз на каждое сообщение.
        data["employee"] = await self._get(user.id) if user and is_private else None
        data["employees_cache"] = self
        if isinstance(event, (Message, CallbackQuery)):
            data["repo"] = self._repo
        return await handler(event, data)
