"""Конфигурация приложения. Всё читается из переменных окружения (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_list(key: str, default: str = "") -> list[str]:
    raw = _env(key, default)
    return [item.strip() for item in raw.split("|") if item.strip()]


def _env_ids(key: str) -> set[int]:
    return {int(x) for x in _env(key).replace(" ", "").split(",") if x.isdigit()}


@dataclass(frozen=True)
class Settings:
    # --- Telegram ---
    bot_token: str = field(default_factory=lambda: _env("BOT_TOKEN"))
    admin_ids: set[int] = field(default_factory=lambda: _env_ids("ADMIN_IDS"))
    # Группа, где коллеги выкладывают договора для извлечения ИНН. Пусто = любая
    # группа, куда добавлен бот (удобно для старта — потом можно сузить до одной).
    inn_group_id: int = field(default_factory=lambda: int(_env("INN_GROUP_CHAT_ID", "0") or "0"))

    # --- Google Диск: автослежение за папкой с договорами ---
    # Пусто = функция выключена. Папку нужно открыть сервисному аккаунту бота
    # (client_email из credentials.json) как минимум с правом «читатель».
    drive_folder_id: str = field(default_factory=lambda: _env("GOOGLE_DRIVE_FOLDER_ID"))
    drive_poll_seconds: int = field(
        default_factory=lambda: int(_env("DRIVE_POLL_SECONDS", "300") or "300")
    )
    drive_notify_chat_id: int = field(
        default_factory=lambda: int(_env("DRIVE_NOTIFY_CHAT_ID", "0") or "0")
    )

    # --- OCR: anthropic | openai | gemini | tesseract ---
    ocr_engine: str = field(default_factory=lambda: _env("OCR_ENGINE", "anthropic").lower())

    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    anthropic_model: str = field(default_factory=lambda: _env("ANTHROPIC_MODEL", "claude-sonnet-5"))

    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    openai_model: str = field(default_factory=lambda: _env("OPENAI_MODEL", "gpt-4o"))

    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    gemini_model: str = field(default_factory=lambda: _env("GEMINI_MODEL", "gemini-2.0-flash"))

    tesseract_lang: str = field(default_factory=lambda: _env("TESSERACT_LANG", "rus+uzb+eng"))
    tesseract_cmd: str = field(default_factory=lambda: _env("TESSERACT_CMD"))

    # --- Google Sheets ---
    spreadsheet_id: str = field(default_factory=lambda: _env("SPREADSHEET_ID"))
    google_credentials_file: str = field(
        default_factory=lambda: _env("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    )
    sheet_data: str = field(default_factory=lambda: _env("SHEET_DATA", "Данные"))
    sheet_employees: str = field(default_factory=lambda: _env("SHEET_EMPLOYEES", "Сотрудники"))
    sheet_projects: str = field(default_factory=lambda: _env("SHEET_PROJECTS", "Проекты"))

    # --- Бизнес-справочники ---
    projects: list[str] = field(
        default_factory=lambda: _env_list(
            "PROJECTS", "🏢 Чукурсой завод|🏗 Ийк Ота завод|⚙️ Цех учун"
        )
    )
    default_currency: str = field(default_factory=lambda: _env("DEFAULT_CURRENCY", "UZS"))
    default_contract_type: str = field(
        default_factory=lambda: _env("DEFAULT_CONTRACT_TYPE", "Местный")
    )

    def validate(self) -> None:
        missing = []
        if not self.bot_token:
            missing.append("BOT_TOKEN")
        if not self.spreadsheet_id:
            missing.append("SPREADSHEET_ID")

        keys = {
            "anthropic": ("ANTHROPIC_API_KEY", self.anthropic_api_key),
            "openai": ("OPENAI_API_KEY", self.openai_api_key),
            "gemini": ("GEMINI_API_KEY", self.gemini_api_key),
        }
        if self.ocr_engine in keys:
            name, value = keys[self.ocr_engine]
            if not value:
                missing.append(name)
        elif self.ocr_engine != "tesseract":
            raise ValueError(
                f"Неизвестный OCR_ENGINE={self.ocr_engine!r}. "
                "Допустимо: anthropic, openai, gemini, tesseract"
            )

        if missing:
            raise ValueError("Не заданы переменные окружения: " + ", ".join(missing))


settings = Settings()
