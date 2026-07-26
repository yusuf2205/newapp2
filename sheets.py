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
    LOG_COLUMNS,
    NOT_SET,
    PROJECT_COLUMNS,
    STATUS_ACTIVE,
    DocumentData,
    amount_cell,
    format_amount,
    parse_amount,
)

logger = logging.getLogger(__name__)

COL_COUNTERPARTY = 2  # B — Наименование контрагента
COL_CONTRACT = 3  # C — Номер контракта с датой
COL_TOTAL = 6  # F — Общая Сумма контракта
COL_PAID = 7  # G — Оплачено
COL_BALANCE = 8  # H — Остаток суммы
COL_NOTE = 11  # K — Примечание
COL_INN = 12  # L — ИНН

# Берём именно код договора после "№" и останавливаемся на первом пробеле — так
# "Дог №ЛТУТ-002252 от 21.07.2026 Доп.Согл №1 от 22.07.2026" даёт код "ЛТУТ-002252",
# а не склеенную строку с датой и номером допсоглашения, из-за которой одинаковые
# договоры считались разными (см. скрин с дублирующимися строками ЛТУТ-002252).
_CONTRACT_CODE_RE = re.compile(r"№\s*([0-9A-Za-zА-Яа-яЁё]+(?:[-/][0-9A-Za-zА-Яа-яЁё]+)*)")

_COMPANY_TOKENS = {
    "ООО", "OOO", "MCHJ", "ЧП", "ИП", "АО", "ЗАО", "ПАО", "ОАО", "ГУП",
    "ХК", "XK", "QK", "ЧЖ", "ФХ", "FX", "MFY",
}
_QUOTE_RE = re.compile(r"[\"'«»“”]")


def normalize_contract(value: str) -> str:
    """'Договор №80 от 08.09.2025' -> '80'; 'договору № 80 от 08.09.2025' -> то же.

    Сравниваем только код договора после «№», а не весь текст: разные документы
    (личный чат через .docx, группа через сканы/PDF) описывают один и тот же договор
    разными словами вокруг номера ("Договор №", "договору №", просто "№"...) и иногда
    дописывают рядом номер допсоглашения/дату — код после первого «№» остаётся общим.
    """
    if not value or value == NOT_SET:
        return ""
    match = _CONTRACT_CODE_RE.search(value)
    code = match.group(1) if match else re.sub(r"[^\d./\-]+", "", value)
    return code.upper()


def normalize_counterparty(value: str) -> str:
    """'"LIDER TEAM" MCHJ' и 'ООО "LIDER TEAM"' -> 'LIDER TEAM'.

    Убираем кавычки и организационно-правовую форму (ООО/MCHJ/...), чтобы не считать
    одну и ту же компанию, записанную по-разному, разными контрагентами.
    """
    if not value or value == NOT_SET:
        return ""
    text = _QUOTE_RE.sub(" ", value.upper())
    tokens = [t for t in text.split() if t not in _COMPANY_TOKENS]
    return " ".join(tokens)


class SheetsRepo:
    """Тонкая обёртка над gspread: синхронные вызовы уходят в отдельный поток."""

    def __init__(
        self,
        credentials_file: str,
        spreadsheet_id: str,
        data_sheet: str,
        employees_sheet: str,
        projects_sheet: str = "Проекты",
        log_sheet: str = "Журнал",
    ) -> None:
        self._credentials_file = credentials_file
        self._spreadsheet_id = spreadsheet_id
        self._data_title = data_sheet
        self._employees_title = employees_sheet
        self._projects_title = projects_sheet
        self._log_title = log_sheet
        self._spreadsheet: gspread.Spreadsheet | None = None
        self._lock = asyncio.Lock()  # защищает read-modify-write при дедупликации
        self._custom_projects: list[str] = []  # кэш листа «Проекты», грузится в init()

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

        current = worksheet.row_values(1)
        if not current:
            worksheet.update("A1", [headers], value_input_option="USER_ENTERED")
            worksheet.freeze(rows=1)
        elif len(current) < len(headers):
            # Схема выросла (например, добавилась колонка «ИНН») — дописываем
            # недостающие заголовки в конец, не трогая уже записанные строки.
            if worksheet.col_count < len(headers):
                worksheet.resize(cols=len(headers))
            missing = headers[len(current):]
            worksheet.update(
                gspread.utils.rowcol_to_a1(1, len(current) + 1), [missing],
                value_input_option="USER_ENTERED",
            )
        return worksheet

    async def init(self) -> None:
        """Создать листы с заголовками, если их ещё нет."""
        data_ws = await asyncio.to_thread(self._worksheet, self._data_title, COLUMNS)
        await asyncio.to_thread(self._worksheet, self._employees_title, EMPLOYEE_COLUMNS)
        await asyncio.to_thread(self._worksheet, self._projects_title, PROJECT_COLUMNS)
        await asyncio.to_thread(self._worksheet, self._log_title, LOG_COLUMNS)
        self._custom_projects = await asyncio.to_thread(self._load_projects)
        await asyncio.to_thread(self._apply_conditional_formatting, data_ws)
        await asyncio.to_thread(self._apply_number_format, data_ws)
        await asyncio.to_thread(self._build_analytics_sheet)

    # ---------- цветовая маркировка долга (лист «Данные») ----------

    def _apply_conditional_formatting(self, worksheet: gspread.Worksheet) -> None:
        """Красный — есть остаток (долг), зелёный — оплачено полностью. Пересоздаём
        правила на каждом старте бота (удаляем старые, добавляем свежие), чтобы не
        плодить дубли при перезапуске."""
        spreadsheet = worksheet.spreadsheet
        metadata = spreadsheet.fetch_sheet_metadata(
            params={"fields": "sheets(properties(sheetId),conditionalFormats)"}
        )
        sheet_meta = next(
            (s for s in metadata["sheets"] if s["properties"]["sheetId"] == worksheet.id), None
        )
        existing_count = len(sheet_meta.get("conditionalFormats", [])) if sheet_meta else 0

        data_range = {
            "sheetId": worksheet.id,
            "startRowIndex": 1,
            "startColumnIndex": 0,
            "endColumnIndex": len(COLUMNS),
        }
        requests = [
            {"deleteConditionalFormatRule": {"sheetId": worksheet.id, "index": i}}
            for i in reversed(range(existing_count))
        ]
        # Суммы пишутся с точкой (Python Decimal), а локаль таблицы ru_RU ждёт запятую —
        # обычное сравнение $H2>0 не работает, т.к. ячейка остаётся текстом. Приводим
        # через SUBSTITUTE+VALUE с IFERROR-фолбэком (не число -> условие ложно).
        debt_value = 'IFERROR(VALUE(SUBSTITUTE($H2;".";","));0)'
        total_value = 'IFERROR(VALUE(SUBSTITUTE($F2;".";","));-1)'
        requests += [
            {  # есть долг: Остаток (H) > 0
                "addConditionalFormatRule": {
                    "index": 0,
                    "rule": {
                        "ranges": [data_range],
                        "booleanRule": {
                            "condition": {
                                "type": "CUSTOM_FORMULA",
                                "values": [{"userEnteredValue": f"={debt_value}>0"}],
                            },
                            "format": {"backgroundColor": {"red": 0.96, "green": 0.80, "blue": 0.80}},
                        },
                    },
                }
            },
            {  # оплачено полностью: сумма известна и Остаток <= 0
                "addConditionalFormatRule": {
                    "index": 1,
                    "rule": {
                        "ranges": [data_range],
                        "booleanRule": {
                            "condition": {
                                "type": "CUSTOM_FORMULA",
                                "values": [
                                    {"userEnteredValue": f"=AND({total_value}>=0;{debt_value}<=0)"}
                                ],
                            },
                            "format": {"backgroundColor": {"red": 0.80, "green": 0.94, "blue": 0.80}},
                        },
                    },
                }
            },
        ]
        spreadsheet.batch_update({"requests": requests})

    # ---------- читаемые суммы (разделитель тысяч) ----------

    def _apply_number_format(self, worksheet: gspread.Worksheet) -> None:
        """«3000000» -> «3 000 000». Работает только если ячейка реально хранит число
        (см. amount_cell в models.py) — на тексте числовой формат не действует."""
        number_format = {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}
        requests = [
            {
                "repeatCell": {
                    "range": {
                        "sheetId": worksheet.id,
                        "startRowIndex": 1,
                        "startColumnIndex": COL_TOTAL - 1,
                        "endColumnIndex": COL_BALANCE,
                    },
                    "cell": {"userEnteredFormat": number_format},
                    "fields": "userEnteredFormat.numberFormat",
                }
            }
        ]
        worksheet.spreadsheet.batch_update({"requests": requests})

    # ---------- лист «Аналитика» ----------

    def _build_analytics_sheet(self) -> None:
        """Простая наглядная аналитика для руководства: крупные итоговые цифры,
        три коротких таблицы и три диаграммы. Пересчитывается сама формулами при
        любом изменении листа «Данные», без участия бота.

        QUERY надёжнее связки UNIQUE+FILTER+SORT: та комбинация даёт #REF!, когда
        несколько независимых формул динамических массивов ссылаются друг на друга
        в одном диапазоне. Поэтому сначала строим «чистое зеркало» нужных колонок
        (каждая — независимый ARRAYFORMULA прямо от листа «Данные», без ссылок на
        другие формулы этого листа) и уже по нему считаем через QUERY. Зеркало —
        техническая часть, прячем колонки N:Q, чтобы не мешать взгляду.
        """
        spreadsheet = self._connect()
        title = "Аналитика"
        try:
            worksheet = spreadsheet.worksheet(title)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=title, rows=1000, cols=30)
        else:
            worksheet.clear()
            if worksheet.col_count < 30:
                worksheet.resize(cols=30)

        d = self._data_title  # имя листа «Данные» — вдруг переименован через .env
        not_set = NOT_SET
        values = {
            # --- «чистое зеркало» строк «Данные» (техническое, колонки скрыты) ---
            "N1": "Поставщик", "O1": "Оплачено", "P1": "Остаток", "Q1": "Проект",
            # Суммы в «Данные» пишутся с точкой как разделителем (Python Decimal), а
            # локаль таблицы ru_RU ожидает запятую — VALUE() без SUBSTITUTE не понимает
            # такие числа и молча даёт 0 через IFERROR. Меняем точку на запятую перед VALUE().
            "N2": f"=ARRAYFORMULA(IF({d}!B2:B1000=\"\";\"\";{d}!B2:B1000))",
            "O2": f"=ARRAYFORMULA(IFERROR(VALUE(SUBSTITUTE({d}!G2:G1000;\".\";\",\"));0))",
            "P2": f"=ARRAYFORMULA(IFERROR(VALUE(SUBSTITUTE({d}!H2:H1000;\".\";\",\"));0))",
            "Q2": f"=ARRAYFORMULA(IF({d}!A2:A1000=\"\";\"\";{d}!A2:A1000))",
            # --- крупные итоговые цифры ---
            "A1": "💰 ОПЛАЧЕНО ВСЕГО", "B1": "=SUM(O2:O1000)",
            "A2": "🔴 ОБЩИЙ ДОЛГ", "B2": '=SUMIF(P2:P1000;">0")',
            # --- три коротких таблицы (данные для диаграмм ниже) ---
            "A4": "📊 Кому платим больше всего",
            "A5": (
                f"=QUERY(N2:Q1000;\"select N, count(N), sum(O), sum(P) "
                f"where N is not null and N <> '' and N <> '{not_set}' "
                f"group by N order by sum(O) desc "
                f"label count(N) 'Договоров', sum(O) 'Оплачено', sum(P) 'Долг'\";0)"
            ),
            "G4": "🔴 Кому мы должны",
            "G5": (
                f"=QUERY(N2:Q1000;\"select N, sum(P) "
                f"where N is not null and N <> '' and N <> '{not_set}' and P > 0 "
                f"group by N order by sum(P) desc "
                f"label sum(P) 'Долг'\";0)"
            ),
            "J4": "🏗 Расходы по проектам",
            "J5": (
                f"=QUERY(N2:Q1000;\"select Q, count(Q), sum(O) "
                f"where Q is not null and Q <> '' and Q <> '{not_set}' "
                f"group by Q order by sum(O) desc "
                f"label count(Q) 'Договоров', sum(O) 'Оплачено'\";0)"
            ),
        }
        worksheet.batch_update(
            [{"range": cell, "values": [[value]]} for cell, value in values.items()],
            value_input_option="USER_ENTERED",
        )

        worksheet.format("N1:Q1", {"textFormat": {"bold": True}})
        worksheet.format("A1:A2", {"textFormat": {"bold": True, "fontSize": 13}})
        worksheet.format("B1:B2", {"textFormat": {"bold": True, "fontSize": 20}})
        worksheet.format("B1", {"backgroundColor": {"red": 0.85, "green": 0.95, "blue": 0.85}})
        worksheet.format("B2", {"backgroundColor": {"red": 0.98, "green": 0.85, "blue": 0.85}})
        worksheet.format("A4", {"textFormat": {"bold": True, "fontSize": 12}})
        worksheet.format("G4", {"textFormat": {"bold": True, "fontSize": 12}})
        worksheet.format("J4", {"textFormat": {"bold": True, "fontSize": 12}})

        number_format = {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}
        for cell_range in ("B1:B2", "C5:D500", "H5:H500", "L5:L500", "O2:P1000"):
            worksheet.format(cell_range, number_format)

        self._hide_columns(worksheet, start=13, end=17)  # N:Q (0-индексовано 13..16)
        self._remove_analytics_charts(worksheet)

    def _hide_columns(self, worksheet: gspread.Worksheet, start: int, end: int) -> None:
        worksheet.spreadsheet.batch_update({"requests": [{
            "updateDimensionProperties": {
                "range": {
                    "sheetId": worksheet.id, "dimension": "COLUMNS",
                    "startIndex": start, "endIndex": end,
                },
                "properties": {"hiddenByUser": True},
                "fields": "hiddenByUser",
            }
        }]})

    def _remove_analytics_charts(self, worksheet: gspread.Worksheet) -> None:
        """Диаграммы не прижились — убираем (если остались от предыдущей версии)."""
        spreadsheet = worksheet.spreadsheet
        metadata = spreadsheet.fetch_sheet_metadata(
            params={"fields": "sheets(properties(sheetId),charts(chartId))"}
        )
        sheet_meta = next(
            (s for s in metadata["sheets"] if s["properties"]["sheetId"] == worksheet.id), None
        )
        existing_charts = sheet_meta.get("charts", []) if sheet_meta else []
        if not existing_charts:
            return
        requests = [{"deleteEmbeddedObject": {"objectId": c["chartId"]}} for c in existing_charts]
        spreadsheet.batch_update({"requests": requests})

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
                    "status": row[4] or STATUS_ACTIVE,
                }
        return None

    async def get_employee(self, telegram_id: int) -> dict | None:
        return await asyncio.to_thread(self._find_employee, telegram_id)

    def _add_employee(self, telegram_id: int, fio: str, position: str, status: str) -> dict:
        worksheet = self._worksheet(self._employees_title, EMPLOYEE_COLUMNS)
        row = [
            str(telegram_id),
            fio,
            position,
            datetime.now().strftime("%d.%m.%Y %H:%M"),
            status,
        ]
        worksheet.append_row(row, value_input_option="USER_ENTERED")
        return {"row": len(worksheet.col_values(1)), "telegram_id": str(telegram_id),
                "fio": fio, "position": position, "date": row[3], "status": status}

    async def add_employee(
        self, telegram_id: int, fio: str, position: str, status: str = STATUS_ACTIVE
    ) -> dict:
        return await asyncio.to_thread(self._add_employee, telegram_id, fio, position, status)

    def _set_employee_status(self, telegram_id: int, status: str) -> dict | None:
        worksheet = self._worksheet(self._employees_title, EMPLOYEE_COLUMNS)
        employee = self._find_employee(telegram_id)
        if employee is None:
            return None
        worksheet.update_cell(employee["row"], 5, status)
        employee["status"] = status
        return employee

    async def set_employee_status(self, telegram_id: int, status: str) -> dict | None:
        """Вернуть обновлённую карточку сотрудника или None, если он не найден."""
        return await asyncio.to_thread(self._set_employee_status, telegram_id, status)

    def _list_employees(self) -> list[dict]:
        worksheet = self._worksheet(self._employees_title, EMPLOYEE_COLUMNS)
        result = []
        for index, row in enumerate(worksheet.get_all_values()[1:], start=2):
            row = row + [""] * (len(EMPLOYEE_COLUMNS) - len(row))
            if not row[0].strip():
                continue
            result.append({
                "row": index, "telegram_id": row[0], "fio": row[1],
                "position": row[2], "date": row[3], "status": row[4] or STATUS_ACTIVE,
            })
        return result

    async def list_employees(self) -> list[dict]:
        return await asyncio.to_thread(self._list_employees)

    # ---------- журнал действий ----------

    def _log_action(self, actor: str, action: str, details: str = "") -> None:
        worksheet = self._worksheet(self._log_title, LOG_COLUMNS)
        worksheet.append_row(
            [datetime.now().strftime("%d.%m.%Y %H:%M"), actor, action, details],
            value_input_option="USER_ENTERED",
        )

    async def log_action(self, actor: str, action: str, details: str = "") -> None:
        try:
            await asyncio.to_thread(self._log_action, actor, action, details)
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось записать в журнал действий: %s / %s", actor, action)

    # ---------- проекты ----------

    def _load_projects(self) -> list[str]:
        worksheet = self._worksheet(self._projects_title, PROJECT_COLUMNS)
        seen: set[str] = set()
        result: list[str] = []
        for value in worksheet.col_values(1)[1:]:
            value = value.strip()
            if value and value.lower() not in seen:
                seen.add(value.lower())
                result.append(value)
        return result

    def custom_projects(self) -> list[str]:
        """Проекты, добавленные через «➕ Другой» (кэш в памяти, свежий с последнего init())."""
        return list(self._custom_projects)

    def _append_project(self, name: str) -> None:
        worksheet = self._worksheet(self._projects_title, PROJECT_COLUMNS)
        worksheet.append_row([name], value_input_option="USER_ENTERED")

    async def add_project(self, name: str) -> None:
        """Запомнить проект, чтобы в следующий раз предложить его в списке."""
        name = name.strip()
        if not name or name.lower() in {p.lower() for p in self._custom_projects}:
            return
        await asyncio.to_thread(self._append_project, name)
        self._custom_projects.append(name)

    # ---------- документы ----------

    def _find_contract_row(
        self, worksheet: gspread.Worksheet, contract: str, counterparty: str = ""
    ) -> int | None:
        target = normalize_contract(contract)
        if not target:
            return None
        target_cp = normalize_counterparty(counterparty)
        contracts = worksheet.col_values(COL_CONTRACT)
        counterparties = worksheet.col_values(COL_COUNTERPARTY) if target_cp else []
        for index, value in enumerate(contracts[1:], start=2):
            if normalize_contract(value) != target:
                continue
            if target_cp:
                # Один и тот же номер договора теоретически могут иметь разные
                # компании — подстраховываемся именем контрагента, если оно известно.
                existing_cp = normalize_counterparty(
                    counterparties[index - 1] if index - 1 < len(counterparties) else ""
                )
                if existing_cp and existing_cp != target_cp:
                    continue
            return index
        return None

    def _upsert(self, doc: DocumentData) -> tuple[int, bool]:
        worksheet = self._worksheet(self._data_title, COLUMNS)
        index = self._find_contract_row(worksheet, doc.contract, doc.counterparty)

        if index is not None:
            # Договор уже есть: прибавляем оплату и пересчитываем остаток
            row = worksheet.row_values(index)
            row += [""] * (len(COLUMNS) - len(row))

            old_paid = parse_amount(row[COL_PAID - 1]) or Decimal(0)
            new_paid = old_paid + (doc.paid or Decimal(0))
            total = parse_amount(row[COL_TOTAL - 1]) or doc.total

            values = [[amount_cell(total), amount_cell(new_paid),
                       amount_cell(total - new_paid if total is not None else None)]]
            worksheet.update(
                f"F{index}:H{index}", values, value_input_option="USER_ENTERED"
            )

            old_note = row[COL_NOTE - 1]
            note = f"{old_note}; " if old_note else ""
            note += f"+{format_amount(doc.paid)} {doc.currency} от {doc.executor}"
            worksheet.update_cell(index, COL_NOTE, note[:1000])
            return index, True

        worksheet.append_row(doc.to_row(), value_input_option="USER_ENTERED")
        return len(worksheet.col_values(COL_CONTRACT)), False

    async def upsert_document(self, doc: DocumentData) -> tuple[int, bool]:
        """Вернуть (номер строки, был ли это апдейт существующего договора)."""
        async with self._lock:
            return await asyncio.to_thread(self._upsert, doc)

    # ---------- договор из группы (сверка со служебными записками) ----------

    def _upsert_contract(self, doc: DocumentData) -> dict:
        """Договор, пришедший в группу: сумма из него авторитетна (перезаписываем
        «Общая сумма контракта»), а «Оплачено» — то, что уже накопилось из служебных
        записок с Диска (см. _upsert) — не трогаем, только пересчитываем остаток."""
        worksheet = self._worksheet(self._data_title, COLUMNS)
        index = self._find_contract_row(worksheet, doc.contract, doc.counterparty)

        if index is not None:
            row = worksheet.row_values(index)
            row += [""] * (len(COLUMNS) - len(row))

            paid = parse_amount(row[COL_PAID - 1]) or Decimal(0)
            total = doc.total if doc.total is not None else parse_amount(row[COL_TOTAL - 1])
            balance = (total - paid) if total is not None else None
            counterparty = doc.counterparty if doc.counterparty != NOT_SET else row[COL_COUNTERPARTY - 1]
            contract = doc.contract if doc.contract != NOT_SET else row[COL_CONTRACT - 1]

            worksheet.update(
                f"B{index}:D{index}",
                [[counterparty, contract, doc.contract_type]],
                value_input_option="USER_ENTERED",
            )
            worksheet.update(
                f"F{index}:H{index}",
                [[amount_cell(total), amount_cell(paid), amount_cell(balance)]],
                value_input_option="USER_ENTERED",
            )
            if doc.inn != NOT_SET:
                worksheet.update_cell(index, COL_INN, doc.inn)

            old_note = row[COL_NOTE - 1]
            note = f"{old_note}; " if old_note else ""
            note += "Сверено по договору из группы"
            worksheet.update_cell(index, COL_NOTE, note[:1000])

            return {
                "row": index, "updated": True, "counterparty": counterparty,
                "total": total, "paid": paid, "balance": balance,
            }

        worksheet.append_row(doc.to_row(), value_input_option="USER_ENTERED")
        return {
            "row": len(worksheet.col_values(COL_CONTRACT)), "updated": False,
            "counterparty": doc.counterparty, "total": doc.total,
            "paid": doc.paid or Decimal(0), "balance": doc.balance,
        }

    async def upsert_contract(self, doc: DocumentData) -> dict:
        """Вернуть {row, updated, counterparty, total, paid, balance} для сообщения в чат."""
        async with self._lock:
            return await asyncio.to_thread(self._upsert_contract, doc)
