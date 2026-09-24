"""One-time setup: tell Telegram to send messages to your Vercel deployment.

Usage:
    python set_webhook.py https://your-project.vercel.app

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET from .env. Run it again
whenever the Vercel URL changes. Running bot.py locally removes the webhook,
so run this again afterwards too.
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from telegram import Bot, Update

load_dotenv()


async def main(base_url: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if not token or not secret:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET in .env first.")

    url = base_url.rstrip("/") + "/api/telegram"
    async with Bot(token) as bot:
        await bot.set_webhook(
            url=url,
            secret_token=secret,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        info = await bot.get_webhook_info()
    print(f"Webhook set to {info.url}")
    if info.last_error_message:
        print(f"Last delivery error reported by Telegram: {info.last_error_message}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].startswith("https://"):
        raise SystemExit("Usage: python set_webhook.py https://your-project.vercel.app")
    asyncio.run(main(sys.argv[1]))
