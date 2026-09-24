"""Vercel entrypoint: Telegram posts each message here (webhook mode).

Telegram sends every update as a POST to /api/telegram. We check the secret
header Telegram adds, run the same handlers bot.py uses, and reply 200 once the
draft has been sent back to the chat.
"""

import hmac
import os

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from telegram import Update

import bot

WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()


async def telegram_webhook(request: Request) -> Response:
    sent = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not WEBHOOK_SECRET or not hmac.compare_digest(sent, WEBHOOK_SECRET):
        return PlainTextResponse("forbidden", status_code=403)

    application = bot.build_application()
    async with application:
        update = Update.de_json(await request.json(), application.bot)
        await application.process_update(update)
    return PlainTextResponse("ok")


async def health(request: Request) -> Response:
    return PlainTextResponse("Meera draft bot is running.")


app = Starlette(
    routes=[
        Route("/", health, methods=["GET"]),
        Route("/api/telegram", telegram_webhook, methods=["POST"]),
    ]
)
