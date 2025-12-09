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

import os

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
        # Если есть медиа (фото, видео, документ, голос и т.д.)
        if msg.media:
            await client.send_file(
                TARGET_CHAT,
                msg.media,
                caption=msg.message or "",
                link_preview=False,
            )
        # Если просто текст
        elif msg.message:
            await client.send_message(
                TARGET_CHAT,
                msg.message,
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

    gid = getattr(event, "grouped_id", None)
    if gid is not None:
        global PROCESSED_ALBUM_IDS
        if gid in PROCESSED_ALBUM_IDS:
            return
        PROCESSED_ALBUM_IDS.add(gid)

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

    print("Клиент запущен. Ожидаю новые сообщения в каналах:")
    for ch in SOURCE_CHANNELS:
        print(f" - {ch}")
    print(f"Целевой чат: {TARGET_CHAT}\n")

    client.run_until_disconnected()


if __name__ == "__main__":
    main()
