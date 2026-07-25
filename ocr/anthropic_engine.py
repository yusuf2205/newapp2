"""OCR через Claude API (нативно понимает и картинки, и PDF)."""

from __future__ import annotations

import base64

from .base import EXTRACTION_PROMPT, OcrEngine, OcrError, extract_json, normalize_mime


class AnthropicOcr(OcrEngine):
    name = "anthropic"

    def __init__(self, api_key: str, model: str = "claude-sonnet-5") -> None:
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model

    async def recognize(self, content: bytes, mime_type: str) -> dict:
        mime_type = normalize_mime(mime_type)
        encoded = base64.standard_b64encode(content).decode()

        if mime_type == "application/pdf":
            block = {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": encoded},
            }
        else:
            block = {
                "type": "image",
                "source": {"type": "base64", "media_type": mime_type, "data": encoded},
            }

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=2000,
                system="Отвечай только валидным JSON, без markdown и комментариев.",
                messages=[{"role": "user", "content": [block, {"type": "text", "text": EXTRACTION_PROMPT}]}],
            )
        except Exception as exc:  # noqa: BLE001
            raise OcrError(f"Ошибка Claude API: {exc}") from exc

        text = "".join(part.text for part in response.content if part.type == "text")
        return extract_json(text)
