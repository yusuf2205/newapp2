"""Разбор таблиц по сетке ячеек (список строк, каждая строка — список текстов ячеек).

Общее для .docx и .xlsx: оба источника дают структурированные ячейки, поэтому логика
поиска бланка заявки на оплату и таблицы-спецификации товаров одна и та же — отличается
только то, как исходный файл превращается в список строк.
"""

from __future__ import annotations

# Ключевые слова для сопоставления заголовков таблицы с полями бланка заявки на оплату.
# "goods" стоит перед "contract": заголовок вида «Предмет договора» должен трактоваться
# как товар, а не как номер договора (иначе он матчился бы по слову "договор" раньше).
COLUMN_KEYWORDS: dict[str, list[str]] = {
    "project": ["заказа", "проект"],
    "counterparty": ["поставщик"],
    "goods": ["товар", "продукц", "предмет", "номенклатур"],
    "contract": ["договор", "контракт"],
    "payment_amount": ["сумма"],
    "payment_note": ["примечание"],
    "executor": ["ответственн"],
    "inn": ["инн", "стир"],
}

# Таблица-спецификация/счёт («Наименование» | «Ед.изм.» | «Кол-во» | «Цена» | «Стоимость»)
# устроена иначе, чем бланк заявки: значения — не одна строка данных под шапкой, а
# позиция на КАЖДОЙ строке таблицы (может быть несколько товаров). Разбираем отдельно.
ITEM_NAME_HEADERS = ("наименование", "товар", "продукц", "описание", "номенклатур")
ITEM_TABLE_SIGNAL_HEADERS = ("кол-во", "количество", "цена", "стоимост", "ед.изм", "ед. изм")
ITEM_ROW_SKIP = {"итого", "всего", "всего к оплате", "jami", "total"}


def match_column(header_text: str) -> str | None:
    normalized = " ".join(header_text.lower().split())
    for key, keywords in COLUMN_KEYWORDS.items():
        if any(keyword in normalized for keyword in keywords):
            return key
    return None


def table_fields_from_grid(rows: list[list[str]]) -> dict:
    """Заголовок с ≥2 узнаваемыми колонками, значения — из первой строки данных
    под ним, по той же позиции в столбце (бланк заявки на оплату)."""
    if len(rows) < 2:
        return {}

    columns = [match_column(cell) for cell in rows[0]]
    if sum(1 for column in columns if column) < 2:
        return {}

    values: dict[str, str] = {}
    for column, cell in zip(columns, rows[1]):
        if not column:
            continue
        text = " ".join(str(cell).split())
        if text:
            values[column] = text
    return values


def goods_from_items_grid(rows: list[list[str]]) -> str | None:
    """Таблица товаров: колонка «Наименование» + рядом «Кол-во»/«Цена»/«Стоимость» —
    собираем названия со всех строк данных, пропуская пустые и итоговые."""
    if len(rows) < 2:
        return None

    headers = [" ".join(str(cell).lower().split()) for cell in rows[0]]
    name_col = next(
        (i for i, h in enumerate(headers) if any(k in h for k in ITEM_NAME_HEADERS)), None
    )
    if name_col is None:
        return None
    if not any(any(k in h for k in ITEM_TABLE_SIGNAL_HEADERS) for h in headers):
        return None  # колонка «Наименование» без цены/количества рядом — не таблица товаров

    names: list[str] = []
    for row in rows[1:]:
        if name_col >= len(row):
            continue
        text = " ".join(str(row[name_col]).split())
        if text and not any(skip in text.lower() for skip in ITEM_ROW_SKIP):
            names.append(text)
    return "; ".join(names) if names else None
