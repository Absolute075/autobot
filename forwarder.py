#!/usr/bin/env python3
"""Telegram userbot: пересылает сообщения из публичных каналов в один целевой канал/группу
без пометки "Переслано от" (создаёт новые сообщения).

Перед использованием:
1. Получите api_id и api_hash на https://my.telegram.org/apps под аккаунтом, который будет userbot'ом.
2. Заполните настройки в блоке CONFIG ниже.
3. Установите зависимости: pip install telethon
4. Первый запуск: python forwarder.py  (ввести номер телефона и код из Telegram).
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta

import aiohttp
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import RPCError

# ==========================
# CONFIG
# ==========================

load_dotenv()

# 1) Данные приложения Telegram
_api_id_env = os.getenv("API_ID")
_api_hash_env = os.getenv("API_HASH")

if not _api_id_env or not _api_hash_env:
    raise RuntimeError("API_ID and API_HASH must be set in .env")

API_ID: int = int(_api_id_env)
API_HASH: str = _api_hash_env

# 2) Каналы-источники
_source_channels_env = os.getenv("SOURCE_CHANNELS")

if not _source_channels_env:
    raise RuntimeError("SOURCE_CHANNELS must be set in .env")

SOURCE_CHANNELS: list[str] = [
    ch.strip()
    for ch in _source_channels_env.split(",")
    if ch.strip()
]

# 3) Целевой чат для пересылки
# - Публичный канал: username БЕЗ @, например "my_target_channel" для @my_target_channel
# - Публичная группа: аналогично
# - Если нужно использовать приватный канал/группу без username, позже можно заменить
#   на numeric ID (например, -1001234567890).
_target_chat_env = os.getenv("TARGET_CHAT")

if not _target_chat_env:
    raise RuntimeError("TARGET_CHAT must be set in .env")

TARGET_CHAT: str | int
if _target_chat_env.lstrip("-").isdigit():
    TARGET_CHAT = int(_target_chat_env)
else:
    TARGET_CHAT = _target_chat_env

# 4) Имя файла сессии (создастся автоматически при первом запуске)
SESSION_NAME: str = os.getenv("SESSION_NAME", "user_session")


FORWARD_ENABLED: bool = True
PROCESSED_ALBUM_IDS: set[int] = set()
PENDING_ALBUMS: dict[int, list] = {}

_usd_to_uzs_rate_env = os.getenv("USD_TO_UZS_RATE")
if _usd_to_uzs_rate_env:
    try:
        USD_TO_UZS_RATE: float | None = float(_usd_to_uzs_rate_env)
    except ValueError:
        USD_TO_UZS_RATE = None
else:
    USD_TO_UZS_RATE = None

EXCHANGE_API_URL: str = os.getenv(
    "EXCHANGERATE_HOST_URL", "https://api.exchangerate.host/live"
)
EXCHANGE_API_KEY: str | None = os.getenv("EXCHANGERATE_HOST_KEY") or None


def _convert_prices(text: str) -> str:
    if not text:
        return text
    if USD_TO_UZS_RATE is None:
        return text

    def _amount_to_uzs(amount_str: str) -> str | None:
        raw = amount_str.replace(" ", "").replace("_", "")
        if not raw:
            return None

        # Удаляем возможные разделители тысяч ("100.000", "100,000")
        digits_only = raw.replace(",", "").replace(".", "")
        if not digits_only.isdigit():
            return None

        amount = int(digits_only)
        uzs = int(round(amount * USD_TO_UZS_RATE))
        return f"{uzs:,}".replace(",", " ")

    # 1) Нормализуем "$400" / "$ 400" в вид "400$"
    def normalize_leading_dollar(match: re.Match[str]) -> str:
        amount_str = match.group("amount")
        return f"{amount_str}$"

    text = re.sub(r"\$\s*(?P<amount>\d[\d\s_.,]*)", normalize_leading_dollar, text)

    # 2) Конвертируем варианты "400$", "400 $", "150 000$", "150 000 $"
    def convert_trailing_dollar(match: re.Match[str]) -> str:
        amount_str = match.group("amount")
        uzs_str = _amount_to_uzs(amount_str)
        if uzs_str is None:
            return match.group(0)
        return f"{uzs_str} UZS"

    original_text = text
    text = re.sub(r"(?P<amount>\d[\d\s_.,]*)\s*\$", convert_trailing_dollar, text)

    # Если мы что-то сконвертировали по доллару, считаем, что это приоритет,
    # и НЕ трогаем остальные числа, чтобы избежать повторной конвертации.
    if text != original_text:
        return text

    def _has_plus_before(s: str, idx: int) -> bool:
        i = idx - 1
        while i >= 0 and s[i].isspace():
            i -= 1
        return i >= 0 and s[i] == "+"

    # 3) Конвертируем большие числа с разделителями разрядов
    #    например: "150 000", "1 200 000", "100.000", "100,000" (без символа $)
    def convert_plain_grouped(match: re.Match[str]) -> str:
        start = match.start()
        if _has_plus_before(text, start):
            return match.group(0)

        amount_str = match.group("amount")
        uzs_str = _amount_to_uzs(amount_str)
        if uzs_str is None:
            return match.group(0)
        return f"{uzs_str} UZS"

    text = re.sub(
        r"(?<!\+)(?P<amount>\d{1,3}(?:[ _.,]\d{3})+)(?!\s*(?:UZS|usd|USD|\$))",
        convert_plain_grouped,
        text,
    )

    # 4) Конвертируем большие числа без разделителей, например: "100000"
    def convert_plain_big(match: re.Match[str]) -> str:
        start = match.start()
        if _has_plus_before(text, start):
            return match.group(0)

        amount_str = match.group("amount")
        uzs_str = _amount_to_uzs(amount_str)
        if uzs_str is None:
            return match.group(0)
        return f"{uzs_str} UZS"

    text = re.sub(
        r"(?<!\+)(?P<amount>\d{5,})(?!\s*(?:UZS|usd|USD|\$))",
        convert_plain_big,
        text,
    )

    return text


async def _update_usd_rate_once() -> None:
    global USD_TO_UZS_RATE

    params: dict[str, str] = {}
    if EXCHANGE_API_KEY:
        params["access_key"] = EXCHANGE_API_KEY
    # Ограничимся одной валютой, если поддерживается
    params["currencies"] = "UZS"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(EXCHANGE_API_URL, params=params, timeout=10) as resp:
                if resp.status != 200:
                    print(f"[WARN] Failed to fetch USD rate: HTTP {resp.status}")
                    return
                data = await resp.json()
    except Exception as e:
        print(f"[WARN] Failed to fetch USD rate: {e}")
        return

    try:
        quotes = data.get("quotes") or {}
        uzs_rate_raw = quotes.get("USDUZS")
        if uzs_rate_raw is None:
            raise KeyError("quotes['USDUZS'] is missing")
        uzs_rate = float(uzs_rate_raw)
    except Exception as e:
        print(f"[WARN] USD->UZS rate not found in response: {e}")
        return

    USD_TO_UZS_RATE = uzs_rate
    print(f"USD_TO_UZS_RATE updated to {USD_TO_UZS_RATE}")


async def _schedule_usd_rate_updates() -> None:
    # Первое обновление сразу после старта
    await _update_usd_rate_once()

    while True:
        now = datetime.now()
        tomorrow = (now + timedelta(days=1)).date()
        next_midnight = datetime.combine(tomorrow, datetime.min.time())
        sleep_seconds = (next_midnight - now).total_seconds()
        if sleep_seconds <= 0:
            sleep_seconds = 24 * 60 * 60

        await asyncio.sleep(sleep_seconds)
        await _update_usd_rate_once()


# ==========================
# ИНИЦИАЛИЗАЦИЯ КЛИЕНТА
# ==========================

client = TelegramClient(SESSION_NAME, API_ID, API_HASH)


# ==========================
# ОБРАБОТЧИК НОВЫХ СООБЩЕНИЙ
# ==========================

@client.on(
    events.NewMessage(
        chats=SOURCE_CHANNELS,
        func=lambda e: not getattr(e.message, "grouped_id", None),
    )
)
async def forward_handler(event: events.NewMessage.Event) -> None:
    """Обработчик новых сообщений в указанных каналах.

    Создаёт новое сообщение в TARGET_CHAT с тем же текстом/медиа,
    поэтому надписи "Переслано от" не будет.
    """

    msg = event.message

    # Игнорируем служебные сообщения (создание чата, закрепление и т.п.)
    if getattr(msg, "action", None):
        return

    if getattr(msg, "grouped_id", None):
        return

    if not FORWARD_ENABLED:
        return

    try:
        text = _convert_prices(msg.message or "")

        # Если есть медиа (фото, видео, документ, голос и т.д.)
        if msg.media:
            await client.send_file(
                TARGET_CHAT,
                msg.media,
                caption=text,
                link_preview=False,
            )
        # Если просто текст
        elif msg.message:
            await client.send_message(
                TARGET_CHAT,
                text,
                link_preview=False,
            )
        # Нечего отправлять
        else:
            return

        src = event.chat.username or event.chat_id
        print(f"Переслано сообщение из {src}")
    except RPCError as e:
        print(f"[ERROR] Ошибка при отправке сообщения: {e}")


@client.on(events.Album(chats=SOURCE_CHANNELS))
async def album_handler(event: events.Album.Event) -> None:
    if not event.messages:
        return

    if not FORWARD_ENABLED:
        return

    files = []
    caption = ""

    for m in event.messages:
        if getattr(m, "action", None):
            continue
        if m.media:
            files.append(m.media)
        if not caption and (m.message or ""):
            caption = m.message

    if not files:
        return

    gid = getattr(event, "grouped_id", None)

    # Если альбом без подписи, откладываем его до появления подписи
    if gid is not None and not caption:
        global PENDING_ALBUMS
        PENDING_ALBUMS[gid] = files
        src = event.chat.username or event.chat_id
        print(f"Получен альбом без описания из {src}, ожидаю подпись")
        return

    if gid is not None:
        global PROCESSED_ALBUM_IDS
        if gid in PROCESSED_ALBUM_IDS:
            return
        PROCESSED_ALBUM_IDS.add(gid)

    caption = _convert_prices(caption)

    try:
        await client.send_file(
            TARGET_CHAT,
            files,
            caption=caption,
            link_preview=False,
        )

        src = event.chat.username or event.chat_id
        print(f"Переслан альбом из {src}")
    except RPCError as e:
        print(f"[ERROR] Ошибка при отправке альбома: {e}")


@client.on(events.MessageEdited(chats=SOURCE_CHANNELS))
async def album_caption_edited_handler(event: events.MessageEdited.Event) -> None:
    if not FORWARD_ENABLED:
        return

    msg = event.message

    if getattr(msg, "action", None):
        return

    gid = getattr(msg, "grouped_id", None)
    if gid is None:
        return

    if not msg.message:
        return

    global PENDING_ALBUMS, PROCESSED_ALBUM_IDS

    files = PENDING_ALBUMS.pop(gid, None)
    if not files:
        return

    if gid in PROCESSED_ALBUM_IDS:
        return

    PROCESSED_ALBUM_IDS.add(gid)

    caption = _convert_prices(msg.message or "")

    try:
        await client.send_file(
            TARGET_CHAT,
            files,
            caption=caption,
            link_preview=False,
        )

        src = event.chat.username or event.chat_id
        print(f"Переслан альбом (по появлению подписи) из {src}")
    except RPCError as e:
        print(f"[ERROR] Ошибка при отправке альбома (edit): {e}")


@client.on(events.NewMessage)
async def control_handler(event: events.NewMessage.Event) -> None:
    msg = event.message

    if not event.is_private:
        return

    if not getattr(msg, "out", False):
        return

    text = (msg.message or "").strip().lower()

    global FORWARD_ENABLED

    if text in {"/stop", "stop", "стоп"}:
        if not FORWARD_ENABLED:
            return
        FORWARD_ENABLED = False
        try:
            await event.reply("Forwarding stopped.")
        except RPCError:
            pass
    elif text in {"/start", "start", "пуск"}:
        if FORWARD_ENABLED:
            return
        FORWARD_ENABLED = True
        try:
            await event.reply("Forwarding started.")
        except RPCError:
            pass


# ==========================
# ТОЧКА ВХОДА
# ==========================

def main() -> None:
    print("=== Telegram userbot forwarder ===")
    print("При первом запуске потребуется ввести номер телефона и код из Telegram.")

    # client.start сам спросит номер/код при первом запуске
    client.start()

    # Фоновое обновление курса USD->UZS (сразу и затем раз в сутки от полуночи)
    client.loop.create_task(_schedule_usd_rate_updates())

    print("Клиент запущен. Ожидаю новые сообщения в каналах:")
    for ch in SOURCE_CHANNELS:
        print(f" - {ch}")
    print(f"Целевой чат: {TARGET_CHAT}\n")

    client.run_until_disconnected()


if __name__ == "__main__":
    main()
