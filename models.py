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
    "ИНН",
]

EMPLOYEE_COLUMNS = ["Telegram ID", "ФИО", "Должность", "Дата", "Статус"]

STATUS_ACTIVE = "Активен"
STATUS_PENDING = "Ожидает подтверждения"
STATUS_BLOCKED = "Заблокирован"
STATUS_ARCHIVED = "В архиве"
STATUS_DENIED = "Отклонено"

PROJECT_COLUMNS = ["Название"]

LOG_COLUMNS = ["Дата и время", "Кто", "Действие", "Детали"]

CURRENCIES = {"UZS", "USD", "EUR", "RUB"}

_NUM_RE = re.compile(r"-?\d[\d\s\u00a0.,']*")
_PERCENT_RE = re.compile(r"(\d{1,3})\s*%")


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
    """Decimal -> '177800000.00'. None -> '[Не указано]'. Для текста в Телеграме."""
    if value is None:
        return NOT_SET
    return f"{value:.2f}"


def amount_cell(value: Decimal | None):
    """Decimal -> float, None -> '[Не указано]'. Для записи В GOOGLE SHEETS: значение
    должно быть настоящим числом (не текстом), иначе числовой формат (разделитель
    тысяч) в таблице не применяется — ячейка с текстом "3000000.00" не форматируется."""
    return float(value) if value is not None else NOT_SET


def _resolve_amounts(data: dict) -> tuple[Decimal | None, Decimal | None]:
    """Обычный документ (счёт/чек/договор): total/paid берутся как распознаны.

    Бланк заявки на оплату (поля «Сумма оплаты в сумах» + «Примечание»): «Примечание»
    может быть '100% оплата', 'предоплата' или, например, '30% оплата'. Для последнего
    случая пересчитываем общую сумму контракта по проценту; для 100%/предоплаты и для
    нераспознанного процента считаем, что сумма оплаты — это и есть вся сумма контракта.
    """
    payment_amount = parse_amount(data.get("payment_amount"))
    if payment_amount is None:
        return parse_amount(data.get("total")), parse_amount(data.get("paid"))

    payment_note = clean_text(data.get("payment_note"), "")
    percent_match = _PERCENT_RE.search(payment_note)
    percent = int(percent_match.group(1)) if percent_match else None

    if percent and 0 < percent < 100:
        total = (payment_amount * Decimal(100) / Decimal(percent)).quantize(Decimal("0.01"))
    else:
        total = payment_amount

    return total, payment_amount


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
    inn: str = NOT_SET
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

        # Тип контракта определяется валютой фактической суммы оплаты, а не тем, что
        # (возможно, ошибочно) вернула модель/эвристика в отдельном поле.
        contract_type = "Импорт" if currency != "UZS" else "Местный"

        total, paid = _resolve_amounts(data)

        return cls(
            project=clean_text(data.get("project")),
            counterparty=clean_text(data.get("counterparty")),
            contract=clean_text(data.get("contract")),
            contract_type=contract_type,
            # Бланк заявки на оплату товар не содержит — там его по-прежнему спросит
            # Телеграм. В обычном договоре (напр. из Google Диска) поле «Предмет
            # договора»/«Наименование товара» есть — если OCR его нашёл, спрашивать
            # уже не нужно.
            goods=clean_text(data.get("goods")),
            total=total,
            paid=paid,
            currency=currency,
            executor=clean_text(data.get("executor")),
            inn=clean_text(data.get("inn")),
            raw_text=str(data.get("raw_text", ""))[:4000],
        )

    def to_row(self) -> list:
        return [
            self.project,
            self.counterparty,
            self.contract,
            self.contract_type,
            self.goods,
            amount_cell(self.total),
            amount_cell(self.paid),
            amount_cell(self.balance),
            self.currency,
            self.executor,
            self.note,
            self.inn,
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
            + (f"\n• ИНН: {self.inn}" if self.inn != NOT_SET else "")
        )
