"""OCR через Google Gemini (нативно принимает и изображения, и PDF)."""

from __future__ import annotations

import asyncio

from .base import EXTRACTION_PROMPT, OcrEngine, OcrError, extract_json, normalize_mime


class GeminiOcr(OcrEngine):
    name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-2.0-flash") -> None:
        from google import genai

        self._genai = genai
        self._client = genai.Client(api_key=api_key)
        self._model = model

    async def recognize(self, content: bytes, mime_type: str) -> dict:
        from google.genai import types

        mime_type = normalize_mime(mime_type)

        def _call():
            return self._client.models.generate_content(
                model=self._model,
                contents=[
                    types.Part.from_bytes(data=content, mime_type=mime_type),
                    EXTRACTION_PROMPT,
                ],
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )

        try:
            response = await asyncio.to_thread(_call)
        except Exception as exc:  # noqa: BLE001
            raise OcrError(f"Ошибка Gemini API: {exc}") from exc

        return extract_json(response.text or "")
