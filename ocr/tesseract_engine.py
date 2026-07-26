"""Локальный OCR через Tesseract — без внешних API.

Tesseract возвращает только плоский текст, поэтому поля вытаскиваются эвристиками
по ключевым словам (рус/узб/англ) через parse_plain_text. Точность заметно ниже
vision-моделей: то, что не удалось распознать, остаётся пустым, и пользователь
дозаполняет это вручную через кнопку «Редактировать».
"""

from __future__ import annotations

import asyncio
import io

from .base import (
    OcrEngine,
    OcrError,
    normalize_mime,
    parse_plain_text,
    pdf_extract_text,
    pdf_to_images,
)

# Если встроенный текстовый слой PDF даёт меньше этого — считаем, что это скан без
# текста (или совсем пустая страница), и уходим на рендер-в-картинку + OCR.
_MIN_NATIVE_TEXT_LENGTH = 40


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

        if mime_type == "application/pdf":
            native_text = await asyncio.to_thread(pdf_extract_text, content)
            if len(native_text.strip()) >= _MIN_NATIVE_TEXT_LENGTH:
                # Цифровой PDF (не скан) — текст точнее и надёжнее любого OCR.
                return parse_plain_text(native_text)

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

        return parse_plain_text(text)
