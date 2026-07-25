"""OCR через OpenAI Vision. PDF предварительно рендерится в PNG-страницы."""

from __future__ import annotations

import base64

from .base import (
    EXTRACTION_PROMPT,
    OcrEngine,
    OcrError,
    extract_json,
    normalize_mime,
    pdf_to_images,
)


class OpenAiOcr(OcrEngine):
    name = "openai"

    def __init__(self, api_key: str, model: str = "gpt-4o") -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    async def recognize(self, content: bytes, mime_type: str) -> dict:
        mime_type = normalize_mime(mime_type)

        if mime_type == "application/pdf":
            pages = [("image/png", page) for page in pdf_to_images(content)]
        else:
            pages = [(mime_type, content)]

        parts = [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{page_mime};base64,{base64.b64encode(data).decode()}",
                    "detail": "high",
                },
            }
            for page_mime, data in pages
        ]
        parts.append({"type": "text", "text": EXTRACTION_PROMPT})

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=2000,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": "Отвечай только валидным JSON."},
                    {"role": "user", "content": parts},
                ],
            )
        except Exception as exc:  # noqa: BLE001
            raise OcrError(f"Ошибка OpenAI API: {exc}") from exc

        return extract_json(response.choices[0].message.content or "")
