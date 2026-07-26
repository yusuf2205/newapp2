"""Автослежение за папкой Google Диска: новые .docx служебные записки обрабатываются
и пишутся в Google Sheets без ручной загрузки в Телеграм.

Работает поллингом (files.list по modifiedTime), а не push-уведомлениями — у бота
нет публичного HTTPS-адреса для вебхука Google Drive. Обработанные файлы запоминаем
в локальном JSON, чтобы не разбирать их повторно после каждого перезапуска.

ingest_new_files() используется и периодическим циклом (main.py), и разовым запросом
из группового потока (inn_group.py) — когда приходит договор, сверка со служебными
записками должна учитывать самые свежие данные, не дожидаясь таймера.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from models import NOT_SET, DocumentData
from ocr.docx_engine import recognize_docx

if TYPE_CHECKING:
    from aiogram import Bot

    from sheets import SheetsRepo

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_STATE_FILE = Path(__file__).with_name("drive_processed.json")


def _load_processed() -> set[str]:
    if not _STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(_STATE_FILE.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return set()


def _save_processed(ids: set[str]) -> None:
    _STATE_FILE.write_text(json.dumps(sorted(ids)), encoding="utf-8")


class DriveWatcher:
    """Синхронные вызовы Drive API — вызывающая сторона уводит их в отдельный поток."""

    def __init__(self, credentials_file: str, folder_id: str) -> None:
        self._credentials_file = credentials_file
        self._folder_id = folder_id
        self._service = None
        self._processed = _load_processed()
        # Периодический цикл и разовый запрос из группы могут пересечься по времени —
        # без лока оба могли бы одновременно скачивать/писать один и тот же файл дважды.
        self.lock = asyncio.Lock()

    def _connect(self):
        if self._service is None:
            creds = Credentials.from_service_account_file(
                self._credentials_file, scopes=_SCOPES
            )
            self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def list_new_files(self) -> list[dict]:
        service = self._connect()
        query = (
            f"'{self._folder_id}' in parents and trashed = false and "
            f"mimeType = '{_DOCX_MIME}'"
        )
        response = (
            service.files()
            .list(q=query, fields="files(id, name, modifiedTime)", pageSize=100)
            .execute()
        )
        return [f for f in response.get("files", []) if f["id"] not in self._processed]

    def download(self, file_id: str) -> bytes:
        service = self._connect()
        request = service.files().get_media(fileId=file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buffer.getvalue()

    def mark_processed(self, file_id: str) -> None:
        self._processed.add(file_id)
        _save_processed(self._processed)


async def ingest_new_files(
    watcher: DriveWatcher,
    repo: "SheetsRepo",
    bot: "Bot",
    notify_chat_id: int,
    default_currency: str,
) -> int:
    """Скачать, распознать и записать в Sheets все ещё не обработанные файлы папки.
    Возвращает количество найденных (новых) файлов."""
    async with watcher.lock:
        files = await asyncio.to_thread(watcher.list_new_files)
        for index, meta in enumerate(files):
            if index:
                # Пауза между файлами — иначе пачка новых файлов упирается в лимит
                # Google Sheets API (Read/Write requests per minute per user).
                await asyncio.sleep(1.5)
            await _ingest_one(watcher, meta, repo, bot, notify_chat_id, default_currency)
        return len(files)


async def _ingest_one(
    watcher: DriveWatcher,
    meta: dict,
    repo: "SheetsRepo",
    bot: "Bot",
    notify_chat_id: int,
    default_currency: str,
) -> None:
    name = meta.get("name", "?")
    try:
        content = await asyncio.to_thread(watcher.download, meta["id"])
        recognized = await recognize_docx(content)
    except Exception:  # noqa: BLE001
        # Файл повреждён/нечитаем — это не изменится при повторной попытке,
        # помечаем обработанным, чтобы не спотыкаться об него на каждом опросе.
        logger.exception("Не удалось прочитать файл с Диска: %s", name)
        watcher.mark_processed(meta["id"])
        if notify_chat_id:
            await bot.send_message(notify_chat_id, f"❌ Не удалось обработать файл «{name}» с Google Диска.")
        return

    doc = DocumentData.from_ocr(recognized, default_currency)
    if doc.executor == NOT_SET:
        doc.executor = "Google Диск (авто)"
    doc.note = f"Автоматически из Google Диска: {name}"

    if doc.contract == NOT_SET and doc.counterparty == NOT_SET:
        # Распознавание в принципе не нашло ключевых полей — повтор не поможет,
        # помечаем обработанным.
        logger.warning("Не удалось распознать ключевые поля файла с Диска: %s", name)
        watcher.mark_processed(meta["id"])
        if notify_chat_id:
            await bot.send_message(
                notify_chat_id,
                f"⚠️ Файл «{name}» с Google Диска: не нашёл ни договора, ни контрагента — "
                "пропустил, проверьте вручную.",
            )
        return

    try:
        row, updated = await repo.upsert_document(doc)
    except Exception:  # noqa: BLE001
        # Запись в Sheets могла не пройти из-за временной ошибки (лимит API, сеть) —
        # НЕ помечаем обработанным, следующий опрос попробует этот файл снова.
        logger.exception("Не удалось записать файл с Диска в Sheets (попробуем ещё раз позже): %s", name)
        if notify_chat_id:
            await bot.send_message(notify_chat_id, f"❌ Не удалось обработать файл «{name}» с Google Диска.")
        return

    watcher.mark_processed(meta["id"])

    if notify_chat_id:
        status = "♻️ Обновлена" if updated else "✅ Новая"
        await bot.send_message(
            notify_chat_id,
            f"📥 Из Google Диска «{name}»:\n"
            f"Договор: {doc.contract}\nКонтрагент: {doc.counterparty}\n"
            f"Товар: {doc.goods}\n"
            f"{status} строка №{row}.",
        )
