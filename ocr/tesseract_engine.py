"""Локальный OCR через Tesseract — без внешних API.

Tesseract возвращает только плоский текст, поэтому поля вытаскиваются эвристиками
по ключевым словам (рус/узб/англ). Точность заметно ниже vision-моделей: то, что
не удалось распознать, остаётся пустым, и пользователь дозаполняет это вручную
через кнопку «Редактировать».
"""

from __future__ import annotations

import asyncio
import io
import re

from .base import OcrEngine, OcrError, normalize_mime, pdf_to_images

COUNTERPARTY_RE = re.compile(
    r"([\"«»\w\-\. ]{2,60}?(?:MCHJ|МЧЖ|OOO|ООО|ЎАЖ|АЖ|ЧП|XK|LLC|LTD|MAS|QK)\b"
    r"|(?:MCHJ|МЧЖ|OOO|ООО|ЧП|LLC|LTD)\s+[\"«»\w\-\. ]{2,60})",
    re.IGNORECASE,
)
CONTRACT_RE = re.compile(
    r"((?:договор|шартнома|контракт|счёт|счет|схет|hisob|invoice|contract)[^\n]{0,60}?"
    r"[№#N]\s*[\w\-/]+[^\n]{0,30})",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"\b(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\b")
TOTAL_RE = re.compile(
    r"(?:итого|всего к оплате|всего|jami|jami summa|umumiy|total|сумма к оплате)"
    r"[^\d\n]{0,20}([\d\s.,'\u00a0]{3,})",
    re.IGNORECASE,
)
PAID_RE = re.compile(
    r"(?:оплачено|оплата|to['`’]?langan|paid|принято|внесено)[^\d\n]{0,20}([\d\s.,'\u00a0]{3,})",
    re.IGNORECASE,
)
CURRENCY_MAP = {
    "UZS": ("uzs", "сум", "so'm", "som", "сўм"),
    "USD": ("usd", "$", "долл"),
    "EUR": ("eur", "€", "евро"),
    "RUB": ("rub", "руб", "₽"),
}


class TesseractOcr(OcrEngine):
    name = "tesseract"

    def __init__(self, lang: str = "rus+uzb+eng", cmd: str = "") -> None:
        try:
            import pytesseract
            from PIL import Image
        except ImportError as exc:  # pragma: no cover
            raise OcrError("Установите pytesseract и Pillow") from exc

        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd

        self._pytesseract = pytesseract
        self._image = Image
        self._lang = lang

    async def recognize(self, content: bytes, mime_type: str) -> dict:
        mime_type = normalize_mime(mime_type)
        pages = pdf_to_images(content) if mime_type == "application/pdf" else [content]

        def _read() -> str:
            chunks = []
            for page in pages:
                image = self._image.open(io.BytesIO(page))
                chunks.append(self._pytesseract.image_to_string(image, lang=self._lang))
            return "\n".join(chunks)

        try:
            text = await asyncio.to_thread(_read)
        except Exception as exc:  # noqa: BLE001
            raise OcrError(f"Ошибка Tesseract: {exc}") from exc

        return self._parse(text)

    def _parse(self, text: str) -> dict:
        counterparty = self._first(COUNTERPARTY_RE, text)

        contract = self._first(CONTRACT_RE, text)
        if contract and not DATE_RE.search(contract):
            date = DATE_RE.search(text)
            if date:
                contract = f"{contract} от {date.group(1)}"

        currency = None
        lowered = text.lower()
        for code, markers in CURRENCY_MAP.items():
            if any(marker in lowered for marker in markers):
                currency = code
                break

        return {
            "counterparty": counterparty,
            "contract": contract,
            "contract_type": "Импорт" if currency and currency != "UZS" else "Местный",
            "goods": None,  # позиции таблицы Tesseract надёжно не разбирает
            "total": self._first(TOTAL_RE, text),
            "paid": self._first(PAID_RE, text),
            "currency": currency,
            "raw_text": text,
        }

    @staticmethod
    def _first(pattern: re.Pattern, text: str) -> str | None:
        match = pattern.search(text)
        if not match:
            return None
        value = " ".join(match.group(1).split())
        return value or None
