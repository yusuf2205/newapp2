"""Модель распознанного документа и нормализация значений."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation

NOT_SET = "[Не указано]"

# Порядок колонок в Google Sheets (лист «Данные»)
COLUMNS = [
    "Наименование проекта",
    "Наименование контрагента",
    "Номер контракта с датой",
    "Тип контракта",
    "Наименование товара",
    "Общая Сумма контракта",
    "Оплачено",
    "Остаток суммы",
    "Валюта",
    "Исполнитель",
    "Примечание",
]

EMPLOYEE_COLUMNS = ["Telegram ID", "ФИО", "Должность", "Дата", "Статус"]

CURRENCIES = {"UZS", "USD", "EUR", "RUB"}

_NUM_RE = re.compile(r"-?\d[\d\s\u00a0.,']*")


def parse_amount(value) -> Decimal | None:
    """'177 800 000,00 сум' / '$1,250.50' -> Decimal. Возвращает None, если числа нет."""
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None

    text = str(value).strip()
    if not text or text == NOT_SET:
        return None

    match = _NUM_RE.search(text)
    if not match:
        return None

    raw = match.group(0)
    raw = raw.replace(" ", "").replace("\u00a0", "").replace("'", "")

    # Определяем десятичный разделитель по последнему знаку с 1-2 цифрами после него
    last_dot, last_comma = raw.rfind("."), raw.rfind(",")
    sep = max(last_dot, last_comma)
    if sep != -1 and len(raw) - sep - 1 in (1, 2):
        integer = re.sub(r"[.,]", "", raw[:sep])
        raw = f"{integer}.{raw[sep + 1:]}"
    else:
        raw = re.sub(r"[.,]", "", raw)

    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def format_amount(value: Decimal | None) -> str:
    """Decimal -> '177800000.00'. None -> '[Не указано]'."""
    if value is None:
        return NOT_SET
    return f"{value:.2f}"


def clean_text(value, fallback: str = NOT_SET) -> str:
    if value is None:
        return fallback
    text = " ".join(str(value).split())
    if not text or text.lower() in {"none", "null", "n/a", "-", "—"}:
        return fallback
    return text


@dataclass
class DocumentData:
    """Одна строка таблицы. Суммы хранятся как Decimal, остальное — как текст."""

    project: str = NOT_SET
    counterparty: str = NOT_SET
    contract: str = NOT_SET
    contract_type: str = "Местный"
    goods: str = NOT_SET
    total: Decimal | None = None
    paid: Decimal | None = None
    currency: str = "UZS"
    executor: str = NOT_SET
    note: str = ""
    raw_text: str = field(default="", repr=False)

    @property
    def balance(self) -> Decimal | None:
        """Остаток = Общая сумма - Оплачено."""
        if self.total is None:
            return None
        return self.total - (self.paid or Decimal(0))

    @classmethod
    def from_ocr(cls, data: dict, default_currency: str = "UZS") -> "DocumentData":
        currency = clean_text(data.get("currency"), default_currency).upper()
        if currency not in CURRENCIES:
            currency = default_currency

        contract_type = clean_text(data.get("contract_type"), "Местный")
        if contract_type.lower() not in {"местный", "импорт"}:
            contract_type = "Местный"
        contract_type = contract_type.capitalize()

        return cls(
            counterparty=clean_text(data.get("counterparty")),
            contract=clean_text(data.get("contract")),
            contract_type=contract_type,
            goods=clean_text(data.get("goods")),
            total=parse_amount(data.get("total")),
            paid=parse_amount(data.get("paid")),
            currency=currency,
            raw_text=str(data.get("raw_text", ""))[:4000],
        )

    def to_row(self) -> list[str]:
        return [
            self.project,
            self.counterparty,
            self.contract,
            self.contract_type,
            self.goods,
            format_amount(self.total),
            format_amount(self.paid),
            format_amount(self.balance),
            self.currency,
            self.executor,
            self.note,
        ]

    def to_state(self) -> dict:
        payload = asdict(self)
        payload["total"] = str(self.total) if self.total is not None else None
        payload["paid"] = str(self.paid) if self.paid is not None else None
        return payload

    @classmethod
    def from_state(cls, payload: dict) -> "DocumentData":
        payload = dict(payload)
        payload["total"] = parse_amount(payload.get("total"))
        payload["paid"] = parse_amount(payload.get("paid"))
        return cls(**payload)

    def as_card(self) -> str:
        """Карточка подтверждения из ТЗ (шаг 3)."""
        cur = self.currency
        return (
            "🔍 <b>Распознаны данные:</b>\n"
            f"• Проект: {self.project}\n"
            f"• Контрагент: {self.counterparty}\n"
            f"• Договор: {self.contract}\n"
            f"• Тип: {self.contract_type}\n"
            f"• Товар: {self.goods}\n"
            f"• Сумма: {format_amount(self.total)} {cur}\n"
            f"• Оплачено: {format_amount(self.paid)} {cur}\n"
            f"• Остаток (Долг): {format_amount(self.balance)} {cur}\n"
            f"• Исполнитель: {self.executor}"
            + (f" ({self.note})" if self.note else "")
        )
