"""Работа с Google Sheets: сотрудники, запись документов, дедупликация по договору."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from decimal import Decimal

import gspread

from models import (
    COLUMNS,
    EMPLOYEE_COLUMNS,
    NOT_SET,
    DocumentData,
    format_amount,
    parse_amount,
)

logger = logging.getLogger(__name__)

COL_CONTRACT = 3  # C — Номер контракта с датой
COL_TOTAL = 6  # F — Общая Сумма контракта
COL_PAID = 7  # G — Оплачено
COL_BALANCE = 8  # H — Остаток суммы


def normalize_contract(value: str) -> str:
    """'Договор №80 от 08.09.2025' -> 'договор80от08092025' для сравнения."""
    if not value or value == NOT_SET:
        return ""
    return re.sub(r"[^0-9a-zа-яёA-ZА-ЯЁ]+", "", value.lower())


class SheetsRepo:
    """Тонкая обёртка над gspread: синхронные вызовы уходят в отдельный поток."""

    def __init__(self, credentials_file: str, spreadsheet_id: str, data_sheet: str, employees_sheet: str) -> None:
        self._credentials_file = credentials_file
        self._spreadsheet_id = spreadsheet_id
        self._data_title = data_sheet
        self._employees_title = employees_sheet
        self._spreadsheet: gspread.Spreadsheet | None = None
        self._lock = asyncio.Lock()  # защищает read-modify-write при дедупликации

    # ---------- инфраструктура ----------

    def _connect(self) -> gspread.Spreadsheet:
        if self._spreadsheet is None:
            client = gspread.service_account(filename=self._credentials_file)
            self._spreadsheet = client.open_by_key(self._spreadsheet_id)
        return self._spreadsheet

    def _worksheet(self, title: str, headers: list[str]) -> gspread.Worksheet:
        spreadsheet = self._connect()
        try:
            worksheet = spreadsheet.worksheet(title)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=title, rows=1000, cols=max(len(headers), 12))
            worksheet.append_row(headers, value_input_option="USER_ENTERED")
            worksheet.freeze(rows=1)
            return worksheet

        if not worksheet.row_values(1):
            worksheet.update("A1", [headers], value_input_option="USER_ENTERED")
            worksheet.freeze(rows=1)
        return worksheet

    async def init(self) -> None:
        """Создать листы с заголовками, если их ещё нет."""
        await asyncio.to_thread(self._worksheet, self._data_title, COLUMNS)
        await asyncio.to_thread(self._worksheet, self._employees_title, EMPLOYEE_COLUMNS)

    # ---------- сотрудники ----------

    def _find_employee(self, telegram_id: int) -> dict | None:
        worksheet = self._worksheet(self._employees_title, EMPLOYEE_COLUMNS)
        ids = worksheet.col_values(1)
        target = str(telegram_id)
        for index, value in enumerate(ids[1:], start=2):
            if value.strip() == target:
                row = worksheet.row_values(index)
                row += [""] * (len(EMPLOYEE_COLUMNS) - len(row))
                return {
                    "row": index,
                    "telegram_id": row[0],
                    "fio": row[1],
                    "position": row[2],
                    "date": row[3],
                    "status": row[4] or "Активен",
                }
        return None

    async def get_employee(self, telegram_id: int) -> dict | None:
        return await asyncio.to_thread(self._find_employee, telegram_id)

    def _add_employee(self, telegram_id: int, fio: str, position: str) -> dict:
        worksheet = self._worksheet(self._employees_title, EMPLOYEE_COLUMNS)
        row = [
            str(telegram_id),
            fio,
            position,
            datetime.now().strftime("%d.%m.%Y %H:%M"),
            "Активен",
        ]
        worksheet.append_row(row, value_input_option="USER_ENTERED")
        return {"row": len(worksheet.col_values(1)), "telegram_id": str(telegram_id),
                "fio": fio, "position": position, "date": row[3], "status": "Активен"}

    async def add_employee(self, telegram_id: int, fio: str, position: str) -> dict:
        return await asyncio.to_thread(self._add_employee, telegram_id, fio, position)

    # ---------- документы ----------

    def _upsert(self, doc: DocumentData) -> tuple[int, bool]:
        worksheet = self._worksheet(self._data_title, COLUMNS)
        target = normalize_contract(doc.contract)

        if target:
            contracts = worksheet.col_values(COL_CONTRACT)
            for index, value in enumerate(contracts[1:], start=2):
                if normalize_contract(value) != target:
                    continue

                # Договор уже есть: прибавляем оплату и пересчитываем остаток
                row = worksheet.row_values(index)
                row += [""] * (len(COLUMNS) - len(row))

                old_paid = parse_amount(row[COL_PAID - 1]) or Decimal(0)
                new_paid = old_paid + (doc.paid or Decimal(0))
                total = parse_amount(row[COL_TOTAL - 1]) or doc.total

                values = [[format_amount(total), format_amount(new_paid),
                           format_amount(total - new_paid if total is not None else None)]]
                worksheet.update(
                    f"F{index}:H{index}", values, value_input_option="USER_ENTERED"
                )

                note = f"{row[-1]}; " if row[-1] else ""
                note += f"+{format_amount(doc.paid)} {doc.currency} от {doc.executor}"
                worksheet.update_cell(index, len(COLUMNS), note[:1000])
                return index, True

        worksheet.append_row(doc.to_row(), value_input_option="USER_ENTERED")
        return len(worksheet.col_values(COL_CONTRACT)), False

    async def upsert_document(self, doc: DocumentData) -> tuple[int, bool]:
        """Вернуть (номер строки, был ли это апдейт существующего договора)."""
        async with self._lock:
            return await asyncio.to_thread(self._upsert, doc)
