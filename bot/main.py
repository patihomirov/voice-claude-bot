"""Voice Claude Bot — entry point."""

import logging
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from .handlers import (
    callback_handler,
    cmd_discuss,
    cmd_go,
    cmd_new,
    cmd_project,
    cmd_projects,
    cmd_start,
    cmd_status,
    cmd_stop,
    handle_document,
    handle_photo,
    handle_text,
    handle_voice,
    set_owner_id,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class OwnerFilter(filters.BaseFilter):
    """Only allow messages from the bot owner."""

    def __init__(self, owner_id: int):
        super().__init__()
        self.owner_id = owner_id

    def filter(self, message) -> bool:
        return message.from_user and message.from_user.id == self.owner_id


async def post_init(app: Application) -> None:
    """Set bot commands after startup."""
    commands = [
        BotCommand("start", "Select project"),
        BotCommand("project", "Switch project"),
        BotCommand("new", "New session"),
        BotCommand("go", "Work mode (allow edits)"),
        BotCommand("discuss", "Discuss mode (read-only)"),
        BotCommand("stop", "Stop Claude"),
        BotCommand("status", "Current status"),
    ]
    await app.bot.set_my_commands(commands)

    owner_id = os.environ.get("TELEGRAM_OWNER_CHAT_ID", "")
    if owner_id:
        await app.bot.send_message(
            chat_id=int(owner_id),
            text="🤖 Voice Claude Bot started.\n/start — select a project",
        )
    logger.info("Bot started, notified owner %s", owner_id)


def main():
    # Load env from .env file
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        load_dotenv(env_file)

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    owner_id_str = os.environ.get("TELEGRAM_OWNER_CHAT_ID")

    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)
    if not owner_id_str:
        logger.error("TELEGRAM_OWNER_CHAT_ID not set")
        sys.exit(1)

    try:
        owner_id = int(owner_id_str)
    except ValueError:
        logger.error("TELEGRAM_OWNER_CHAT_ID must be an integer, got: %s", owner_id_str)
        sys.exit(1)
    owner_filter = OwnerFilter(owner_id)
    set_owner_id(owner_id)

    # Force IPv4 to avoid Telegram API timeouts via IPv6
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    request = HTTPXRequest(
        httpx_kwargs={"transport": transport},
        connect_timeout=10.0,
        read_timeout=65.0,
    )
    get_updates_request = HTTPXRequest(
        httpx_kwargs={"transport": httpx.AsyncHTTPTransport(local_address="0.0.0.0")},
        connect_timeout=10.0,
        read_timeout=65.0,
    )

    app = (
        Application.builder()
        .token(token)
        .request(request)
        .get_updates_request(get_updates_request)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", cmd_start, filters=owner_filter))
    app.add_handler(CommandHandler("project", cmd_project, filters=owner_filter))
    app.add_handler(CommandHandler("projects", cmd_projects, filters=owner_filter))
    app.add_handler(CommandHandler("new", cmd_new, filters=owner_filter))
    app.add_handler(CommandHandler("go", cmd_go, filters=owner_filter))
    app.add_handler(CommandHandler("discuss", cmd_discuss, filters=owner_filter))
    app.add_handler(CommandHandler("stop", cmd_stop, filters=owner_filter))
    app.add_handler(CommandHandler("status", cmd_status, filters=owner_filter))

    # Callbacks (inline buttons) — owner-only via check inside handler
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Messages
    app.add_handler(MessageHandler(
        owner_filter & filters.VOICE,
        handle_voice,
    ))
    app.add_handler(MessageHandler(
        owner_filter & filters.PHOTO,
        handle_photo,
    ))
    app.add_handler(MessageHandler(
        owner_filter & filters.Document.ALL,
        handle_document,
    ))
    app.add_handler(MessageHandler(
        owner_filter & filters.TEXT & ~filters.COMMAND,
        handle_text,
    ))

    logger.info("Starting polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
