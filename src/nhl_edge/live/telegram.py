"""Messages to the owner through their Telegram bot (#268): the day's real bets after the 12:45 ET
decision. The bot's token and the owner's chat id come from TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID, and never appear in an error: the workflows' logs are public.
"""

import httpx

from nhl_edge.settings import require

API = "https://api.telegram.org"
# Telegram's limit on one message's text.
MAX_LENGTH = 4096


class TelegramError(RuntimeError):
    """A message Telegram didn't take. Messages carry the status and Telegram's description,
    never the token or the chat id."""


def send(
    text: str,
    token: str | None = None,
    chat_id: str | None = None,
    client: httpx.Client | None = None,
) -> None:
    """Send text as one plain message (no markup to escape)."""
    if len(text) > MAX_LENGTH:
        raise TelegramError(f"the message has {len(text)} characters, over {MAX_LENGTH}")
    token = token or require("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or require("TELEGRAM_CHAT_ID")
    http = client or httpx.Client(timeout=30)
    try:
        response = http.post(
            f"{API}/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"},
        )
    except httpx.HTTPError as exc:
        # The exception's text holds the URL, and so the token.
        raise TelegramError(f"sendMessage failed: {type(exc).__name__}") from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or not body.get("ok"):
        description = str(body.get("description", ""))[:200]
        raise TelegramError(f"sendMessage returned {response.status_code}: {description}")
