"""Базовый интерфейс OCR-движка + утилиты, общие для всех реализаций."""

from __future__ import annotations

import abc
import io
import json
import re

EXTRACTION_PROMPT = """Ты — OCR-парсер финансовых документов (счета, счета-фактуры, договоры,
чеки, платёжные поручения) на русском, узбекском и английском языках.

Извлеки данные из документа и верни СТРОГО один JSON-объект без markdown и пояснений:

{
  "counterparty": "название компании-поставщика/продавца, напр. SPACE POWER MCHJ",
  "contract": "номер и дата документа, напр. Договор №80 от 08.09.2025",
  "contract_type": "Местный или Импорт",
  "goods": "наименование товара/услуги; если позиций много — перечисли через запятую",
  "total": "общая сумма по документу, только число",
  "paid": "фактически оплаченная сумма, только число; null если это не чек/платёжка",
  "currency": "UZS, USD, EUR или RUB",
  "raw_text": "весь распознанный текст документа"
}

Правила:
- Ничего не выдумывай. Если поле не читается или отсутствует — поставь null.
- Суммы: только число, без валюты и пробелов, десятичный разделитель — точка (177800000.00).
- "total" — это итоговая сумма (Итого / Jami / Всего к оплате), включая НДС.
- "paid" заполняй только если документ подтверждает оплату (чек, квитанция, платёжное поручение).
  Для счёта или договора "paid" = null.
- "contract_type" = "Импорт", если контрагент иностранный или валюта не UZS, иначе "Местный".
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
