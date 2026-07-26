"""Базовый интерфейс OCR-движка + утилиты, общие для всех реализаций."""

from __future__ import annotations

import abc
import io
import json
import re

EXTRACTION_PROMPT = """Ты — парсер финансовых документов на русском, узбекском и английском
языках. Бывает два вида документов:

A) Внутренний бланк заявки на оплату с полями (могут называться чуть иначе, но по смыслу
   такими же): «№ Заказа/Проект», «Поставщик», «Договор/контракт», «Сумма оплаты в сумах»,
   «Примечание», «Ответственный».
B) Обычный документ без такой разметки: счёт, счёт-фактура, договор, чек, платёжное поручение.

Извлеки данные и верни СТРОГО один JSON-объект без markdown и пояснений:

{
  "project": "поле «№ Заказа/Проект» бланка A дословно, иначе null",
  "counterparty": "поле «Поставщик» бланка A, иначе название компании-контрагента из
                    документа B, напр. SPACE POWER MCHJ",
  "contract": "поле «Договор/контракт» бланка A, иначе номер и дата документа B,
               напр. Договор №80 от 08.09.2025",
  "executor": "поле «Ответственный» бланка A дословно, иначе null",
  "inn": "ИНН/СТИР контрагента (обычно 9 цифр), если он есть в документе — в бланке A
          и в документе B; только цифры; иначе null. В счетах-фактурах бывает ДВА
          ИНН — поставщика и покупателя (обычно нашей же компании NURA-HIGH-TECHNOLOGIES)
          — бери именно ИНН ПОСТАВЩИКА, не покупателя",
  "goods": "наименование товара/продукции/услуги в документе B (обычный договор) — из поля
            «Предмет договора»/«Наименование товара», либо из таблицы-спецификации со
            столбцом «Наименование» (рядом обычно «Ед.изм.», «Кол-во», «Цена», «Стоимость»);
            если товаров несколько — перечисли через '; '. Иначе null. Не путать с шапкой
            бланка A — там такого поля нет",
  "doc_number": "код документа вида 'СО-0736 24.07.2026' (буквы-дефис-цифры, часто с датой
                 рядом), если он есть где-либо в документе, дословно; иначе null",
  "payment_amount": "поле «Сумма оплаты в сумах» бланка A — только число, иначе null",
  "payment_note": "поле «Примечание» бланка A ДОСЛОВНО (напр. '100% оплата', 'предоплата',
                   '30% оплата'), иначе null",
  "total": "для документа B — общая сумма (Итого / Jami / Всего к оплате), только число;
            null для бланка A",
  "paid": "для документа B — фактически оплаченная сумма, только число, null если документ
           не подтверждает оплату (счёт/договор); null для бланка A",
  "currency": "UZS, USD, EUR или RUB — по фактической сумме оплаты/итога, а не по названию
               поля (если «Сумма оплаты в сумах» на деле указана в долларах — это USD)",
  "raw_text": "весь распознанный текст документа"
}

Правила:
- Ничего не выдумывай. Если поле не читается или отсутствует — поставь null.
- Суммы: только число, без валюты и пробелов, десятичный разделитель — точка (177800000.00).
- Не путай бланк A и документ B: у бланка A всегда null в total/paid, у документа B —
  null в project/executor/payment_amount/payment_note.
- В начале служебных записок часто есть шапка «Директору ... ООО «Наша компания»» —
  это адресат (получатель записки, наша организация), а НЕ контрагент/поставщик.
  Никогда не бери название из этой шапки как "counterparty".
"""


class OcrError(RuntimeError):
    """Ошибка распознавания документа."""


class OcrEngine(abc.ABC):
    """Единый интерфейс: подаём байты файла, получаем словарь полей."""

    name: str = "base"

    @abc.abstractmethod
    async def recognize(self, content: bytes, mime_type: str) -> dict:
        """Вернуть словарь с ключами counterparty/contract/goods/total/paid/currency/raw_text."""
        raise NotImplementedError


def extract_json(text: str) -> dict:
    """Достать JSON из ответа модели, даже если он обёрнут в ```json ... ```."""
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        raise OcrError("Модель вернула ответ без JSON")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise OcrError(f"Не удалось разобрать JSON: {exc}") from exc


def pdf_extract_text(content: bytes, max_pages: int = 5) -> str:
    """Достаём встроенный текстовый слой PDF напрямую, без OCR — так же надёжно, как
    .docx, если документ сгенерирован цифровым способом (типичные ЭДО-счета-фактуры,
    не сканы). Возвращает '' если слоя нет или он пустой — тогда вызывающий код должен
    сам откатиться на рендер-в-картинку + OCR (см. pdf_to_images)."""
    try:
        import pypdfium2 as pdfium
    except ImportError:  # pragma: no cover
        return ""

    try:
        pdf = pdfium.PdfDocument(content)
        parts = []
        for index in range(min(len(pdf), max_pages)):
            textpage = pdf[index].get_textpage()
            parts.append(textpage.get_text_range())
        return "\n".join(parts)
    except Exception:  # noqa: BLE001
        return ""


def pdf_to_images(content: bytes, max_pages: int = 5, dpi: int = 200) -> list[bytes]:
    """PDF -> список PNG-страниц. Нужен для движков без нативной поддержки PDF."""
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover
        raise OcrError("Для обработки PDF установите pypdfium2") from exc

    pdf = pdfium.PdfDocument(content)
    images: list[bytes] = []
    for index in range(min(len(pdf), max_pages)):
        page = pdf[index]
        pil_image = page.render(scale=dpi / 72).to_pil()
        buffer = io.BytesIO()
        pil_image.save(buffer, format="PNG")
        images.append(buffer.getvalue())
    return images


COUNTERPARTY_RE = re.compile(
    r"([\"«»\w\-\. ]{2,60}?(?:MCHJ|МЧЖ|OOO|ООО|ЎАЖ|АЖ|ЧП|XK|LLC|LTD|MAS|QK)\b"
    r"|(?:MCHJ|МЧЖ|OOO|ООО|ЧП|LLC|LTD)\s+[\"«»\w\-\. ]{2,60})",
    re.IGNORECASE,
)
CONTRACT_RE = re.compile(
    r"((?:договор|шартнома|контракт|счёт|счет|схет|hisob|invoice|contract)[^\n]{0,8}?"
    r"[№#N][ \t]*[\w\-/]+[^\n]{0,20})",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"\b(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\b")
TOTAL_RE = re.compile(
    r"(?:итого|всего к оплате|всего|jami|jami summa|umumiy|total|сумма к оплате)"
    r"[^\d\n]{0,20}([\d ,.' ]{3,})",
    re.IGNORECASE,
)
PAID_RE = re.compile(
    r"(?:оплачено|оплата|to['`’]?langan|paid|принято|внесено)[^\d\n]{0,20}([\d ,.' ]{3,})",
    re.IGNORECASE,
)
CURRENCY_MAP = {
    "UZS": ("uzs", "сум", "so'm", "som", "сўм"),
    "USD": ("usd", "$", "долл"),
    "EUR": ("eur", "€", "евро"),
    "RUB": ("rub", "руб", "₽"),
}


# Метки полей внутреннего бланка заявки на оплату — надёжнее общих эвристик выше,
# т.к. это буквальные подписи полей формы, а не догадки по ключевым словам.
PROJECT_LABEL_RE = re.compile(r"№?\s*Заказа\s*/\s*Проект[^\n:]{0,10}[:\-][ \t]*([^\n]+)", re.IGNORECASE)
SUPPLIER_LABEL_RE = re.compile(r"Поставщик[^\n:]{0,10}[:\-][ \t]*([^\n]+)", re.IGNORECASE)
CONTRACT_LABEL_RE = re.compile(r"Договор\s*/\s*контракт[^\n:]{0,10}[:\-][ \t]*([^\n]+)", re.IGNORECASE)
RESPONSIBLE_LABEL_RE = re.compile(r"Ответственный[^\n:]{0,10}[:\-][ \t]*([^\n]+)", re.IGNORECASE)
PAYMENT_NOTE_LABEL_RE = re.compile(r"Примечание[^\n:]{0,10}[:\-][ \t]*([^\n]+)", re.IGNORECASE)
PAYMENT_AMOUNT_LABEL_RE = re.compile(
    r"Сумма[ \t]+оплаты[ \t]+в[ \t]+сумах[^\n:]{0,10}[:\-][ \t]*([\d ,.'$]{3,})", re.IGNORECASE
)
INN_LABEL_RE = re.compile(r"(?:ИНН|СТИР|STIR|TIN)[^\n\d]{0,10}(\d{7,12})", re.IGNORECASE)

# Счета-фактуры содержат ДВА ИНН — поставщика и покупателя (обычно нашей же компании).
# Ищем именно ИНН поставщика в первую очередь, иначе рискуем случайно взять свой же ИНН.
# Метка иногда переносится на две строки ("Идентификационный номер\nпоставщика (ИНН):"),
# поэтому между словами и до цифр разрешаем перенос строки, но с ограничением длины.
SUPPLIER_INN_LABEL_RE = re.compile(
    r"(?:идентификационный\s+номер\s+поставщика|инн\s+поставщика|стир\s+поставщика)"
    r"[\s\S]{0,40}?(\d{7,12})",
    re.IGNORECASE,
)
BUYER_INN_LABEL_RE = re.compile(
    r"(?:идентификационный\s+номер\s+покупателя|инн\s+покупателя|стир\s+покупателя)"
    r"[\s\S]{0,40}?(\d{7,12})",
    re.IGNORECASE,
)

# Договоры (в отличие от бланка заявки на оплату) обычно называют предмет поставки
# явной подписью — извлекаем её, чтобы не спрашивать товар в Телеграме, если он уже
# есть в самом документе.
GOODS_LABEL_RE = re.compile(
    r"(?:Наименование\s+товара|Наименование\s+продукции|Предмет\s+договора|Предмет\s+поставки|"
    r"Номенклатура)[^\n:]{0,10}[:\-][ \t]*([^\n]+)",
    re.IGNORECASE,
)

# Служебные записки начинаются с «Директору ... ООО «Наша компания»» — это адресат
# (всегда наша организация), а не поставщик. Вырезаем этот блок перед поиском
# контрагента, чтобы не зацепить название своей же фирмы вместо реального поставщика.
ADDRESSEE_BLOCK_RE = re.compile(r"Директору\b.{0,200}", re.IGNORECASE | re.DOTALL)

# Код документа вида "СО-0736 24.07.2026" — не подписан отдельной меткой, ищем сам паттерн.
DOC_NUMBER_RE = re.compile(
    r"\bСО[-‐‑‒–—―]\d+(?:\s+\d{1,2}[./]\d{1,2}[./]\d{2,4})?", re.IGNORECASE
)


def _first_match(pattern: re.Pattern, text: str) -> str | None:
    match = pattern.search(text)
    if not match:
        return None
    value = " ".join(match.group(1).split())
    return value or None


def parse_plain_text(text: str) -> dict:
    """Эвристики по ключевым словам для движков без понимания структуры документа
    (Tesseract, .docx) — надёжность заметно ниже vision-моделей.

    Сначала пробуем метки бланка заявки на оплату (№ Заказа/Проект, Поставщик,
    Договор/контракт, Сумма оплаты в сумах, Примечание, Ответственный) — они точнее,
    т.к. это подписи конкретных полей. Если не нашли — общие эвристики по ключевым
    словам для произвольных счетов/чеков/договоров.
    """
    # \r (из \r\n на Windows) не входит в [^\n] и "просачивается" через него, из-за
    # чего захват "до конца строки" в regex ниже мог утекать на следующую строку.
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    project = _first_match(PROJECT_LABEL_RE, text)
    executor = _first_match(RESPONSIBLE_LABEL_RE, text)
    payment_amount = _first_match(PAYMENT_AMOUNT_LABEL_RE, text)
    payment_note = _first_match(PAYMENT_NOTE_LABEL_RE, text)

    doc_number_match = DOC_NUMBER_RE.search(text)
    doc_number = " ".join(doc_number_match.group(0).split()) if doc_number_match else None

    # Счёт-фактура: явная метка «ИНН поставщика» приоритетнее общей «ИНН», иначе рискуем
    # захватить ИНН покупателя (обычно это наша же компания) вместо контрагента.
    buyer_inn = _first_match(BUYER_INN_LABEL_RE, text)
    inn = _first_match(SUPPLIER_INN_LABEL_RE, text)
    if not inn:
        candidate = _first_match(INN_LABEL_RE, text)
        if candidate and candidate != buyer_inn:
            inn = candidate
    goods = _first_match(GOODS_LABEL_RE, text)

    # Контрагента ищем в тексте без адресата шапки («Директору ... ООО ...») —
    # иначе легко спутать нашу же компанию с реальным поставщиком.
    text_without_addressee = ADDRESSEE_BLOCK_RE.sub(" ", text)
    counterparty = _first_match(SUPPLIER_LABEL_RE, text_without_addressee) or _first_match(
        COUNTERPARTY_RE, text_without_addressee
    )

    contract = _first_match(CONTRACT_LABEL_RE, text) or _first_match(CONTRACT_RE, text)
    if contract and not DATE_RE.search(contract):
        date = DATE_RE.search(text)
        if date:
            contract = f"{contract} от {date.group(1)}"

    currency = None
    lowered = (payment_amount or text).lower()
    for code, markers in CURRENCY_MAP.items():
        if any(marker in lowered for marker in markers):
            currency = code
            break

    return {
        "project": project,
        "counterparty": counterparty,
        "contract": contract,
        "executor": executor,
        "inn": inn,
        "goods": goods,
        "payment_amount": payment_amount,
        "payment_note": payment_note,
        "doc_number": doc_number,
        "total": _first_match(TOTAL_RE, text),
        "paid": _first_match(PAID_RE, text),
        "currency": currency,
        "raw_text": text,
    }


def normalize_mime(mime_type: str) -> str:
    """Telegram иногда присылает image/jpg — приводим к валидному media type."""
    mime_type = (mime_type or "image/jpeg").lower()
    if mime_type == "image/jpg":
        return "image/jpeg"
    if mime_type not in {"image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf"}:
        return "image/jpeg"
    return mime_type


def get_engine(settings) -> OcrEngine:
    """Фабрика движков. Переключение — через OCR_ENGINE в .env."""
    engine = settings.ocr_engine

    if engine == "anthropic":
        from .anthropic_engine import AnthropicOcr

        return AnthropicOcr(settings.anthropic_api_key, settings.anthropic_model)

    if engine == "openai":
        from .openai_engine import OpenAiOcr

        return OpenAiOcr(settings.openai_api_key, settings.openai_model)

    if engine == "gemini":
        from .gemini_engine import GeminiOcr

        return GeminiOcr(settings.gemini_api_key, settings.gemini_model)

    if engine == "tesseract":
        from .tesseract_engine import TesseractOcr

        return TesseractOcr(settings.tesseract_lang, settings.tesseract_cmd)

    raise ValueError(f"Неизвестный OCR-движок: {engine}")
