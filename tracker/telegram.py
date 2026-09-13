"""Envío de alertas por Telegram."""
from . import config
from .net import get_json, request

API = "https://api.telegram.org/bot{token}/{method}"


def _url(method: str) -> str:
    return API.format(token=config.TELEGRAM_BOT_TOKEN, method=method)


def is_configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def send_message(text: str, chat_id: str | None = None) -> None:
    request(_url("sendMessage"), data={
        "chat_id": chat_id or config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


def discover_chats() -> dict[int, str]:
    """Chats que le han escrito al bot (requiere mandarle un mensaje antes)."""
    data, _ = get_json(_url("getUpdates"))
    chats = {}
    for update in data.get("result", []):
        for key in ("message", "channel_post", "my_chat_member", "edited_message"):
            chat = (update.get(key) or {}).get("chat")
            if chat:
                name = chat.get("title") or " ".join(
                    x for x in (chat.get("first_name"), chat.get("last_name")) if x
                ) or chat.get("username") or ""
                chats[chat["id"]] = f"{name} ({chat.get('type')})"
    return chats
