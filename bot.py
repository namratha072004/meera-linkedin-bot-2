"""Telegram bot: turns Meera's voice/text notes into LinkedIn post drafts.

Flow: voice note -> Gemini transcription -> Gemini draft (with the voice skill
as system instruction) -> full output sent back to the same chat. Never posts
anywhere.
"""

import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
# httpx logs every Telegram poll at INFO, which buries everything else.
logging.getLogger("httpx").setLevel(logging.WARNING)
# The Gemini SDK logs on every call, and warns that it skipped "thought" parts.
logging.getLogger("google_genai").setLevel(logging.WARNING)
logging.getLogger("google_genai.types").setLevel(logging.ERROR)
log = logging.getLogger("meera-bot")

TELEGRAM_LIMIT = 4096


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing {name} in .env (see .env.example)")
    return value


TELEGRAM_BOT_TOKEN = require_env("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = require_env("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
SKILL_PATH = Path(
    os.getenv("VOICE_SKILL_PATH", Path(__file__).parent / "meera_voice_skill.md")
)
if not SKILL_PATH.is_file():
    raise SystemExit(f"Voice skill file not found: {SKILL_PATH}")
VOICE_SKILL = SKILL_PATH.read_text(encoding="utf-8")

gemini = genai.Client(api_key=GEMINI_API_KEY).aio


# ---------------------------------------------------------------------------
# Transcription and drafting
# ---------------------------------------------------------------------------


class GeminiError(Exception):
    pass


def response_text(response: types.GenerateContentResponse, what: str) -> str:
    """Return the text of a Gemini response, or raise a plain-English error."""
    feedback = response.prompt_feedback
    if feedback and feedback.block_reason:
        raise GeminiError(f"Gemini blocked the {what} ({feedback.block_reason.name}).")
    candidate = response.candidates[0] if response.candidates else None
    reason = candidate.finish_reason if candidate else None
    text = (response.text or "").strip()
    if not text:
        detail = f" ({reason.name})" if reason else ""
        raise GeminiError(f"Gemini returned an empty {what}{detail}.")
    if reason == types.FinishReason.MAX_TOKENS:
        text += "\n\n[Note: the response hit the length limit and may be cut off.]"
    return text


async def transcribe(audio: bytes, mime_type: str) -> str:
    response = await gemini.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=audio, mime_type=mime_type),
            "Transcribe this voice note word for word. Return only the "
            "transcript, with no introduction, labels or commentary.",
        ],
    )
    return response_text(response, "transcript")


async def draft_post(note: str) -> str:
    response = await gemini.models.generate_content(
        model=GEMINI_MODEL,
        contents=(
            f"NOTE:\n{note}\n\n"
            "CURRENT ANGLE: none provided.\n\n"
            "Write one LinkedIn post following the skill, and return "
            "all four sections in the output format it specifies."
        ),
        config=types.GenerateContentConfig(system_instruction=VOICE_SKILL),
    )
    return response_text(response, "draft")


# ---------------------------------------------------------------------------
# Splitting long replies for Telegram
# ---------------------------------------------------------------------------

# Section headings from the skill's output format, e.g. "**DRAFT**".
SECTION_START = re.compile(r"^(?=\**\s*(?:DRAFT|CHECK BEFORE POSTING|NEWS ANGLE|CORE IDEA)\b)", re.M)


def tg_len(text: str) -> int:
    # Telegram counts UTF-16 code units, so an emoji can count as 2.
    return len(text.encode("utf-16-le")) // 2


def _pack(pieces: list[str], sep: str, limit: int) -> list[str]:
    """Greedily join pieces with sep into chunks no longer than limit."""
    chunks, current = [], ""
    for piece in pieces:
        candidate = f"{current}{sep}{piece}" if current else piece
        if tg_len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks


def _split_oversized(text: str, limit: int) -> list[str]:
    """Split one block that is too long: by paragraph, then line, then word."""
    for sep in ("\n\n", "\n", " "):
        pieces = text.split(sep)
        if len(pieces) == 1:
            continue
        out = []
        for chunk in _pack(pieces, sep, limit):
            out.extend(_split_oversized(chunk, limit) if tg_len(chunk) > limit else [chunk])
        return out
    # A single "word" longer than the limit: nothing left but a hard cut.
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def split_for_telegram(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split text into messages, keeping whole sections together when they fit."""
    if tg_len(text) <= limit:
        return [text]
    sections = [s.strip() for s in SECTION_START.split(text) if s.strip()]
    chunks: list[str] = []
    fitting: list[str] = []
    for section in sections:
        if tg_len(section) <= limit:
            fitting.append(section)
            continue
        chunks.extend(_pack(fitting, "\n\n", limit))
        fitting = []
        chunks.extend(_split_oversized(section, limit))
    chunks.extend(_pack(fitting, "\n\n", limit))
    return chunks


async def reply_long(update: Update, text: str) -> None:
    for chunk in split_for_telegram(text):
        await update.effective_message.reply_text(chunk)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Send me a voice note or a text note and I'll reply with a LinkedIn "
        "draft in your voice. I never post anything - you review and publish."
    )


async def handle_note(note: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        draft = await draft_post(note)
    except GeminiError as e:
        await update.effective_message.reply_text(f"Drafting failed: {e}")
        return
    except genai_errors.APIError as e:
        log.exception("Gemini API error")
        await update.effective_message.reply_text(
            f"Drafting failed: the Gemini API returned an error ({e.code} {e.status}). "
            "Try sending the note again in a minute."
        )
        return
    await reply_long(update, draft)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_note(update.effective_message.text, update, context)


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    media = msg.voice or msg.audio
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    try:
        tg_file = await media.get_file()
        audio = bytes(await tg_file.download_as_bytearray())
    except Exception:
        log.exception("Telegram download error")
        await msg.reply_text("Couldn't download the voice note from Telegram. Please send it again.")
        return

    # Telegram voice notes are Opus in an Ogg container.
    mime_type = getattr(media, "mime_type", None) or "audio/ogg"
    try:
        note = await transcribe(audio, mime_type)
    except GeminiError as e:
        await msg.reply_text(f"Transcription failed: {e} Was the voice note silent? You can also send it as text.")
        return
    except genai_errors.APIError as e:
        log.exception("Gemini transcription error")
        await msg.reply_text(
            f"Transcription failed: the Gemini API returned an error ({e.code} {e.status}). "
            "Try again, or send the note as text."
        )
        return

    # Show what Gemini heard, so a mishearing is easy to spot before reading the draft.
    await reply_long(update, f"Transcript:\n{note}")
    await handle_note(note, update, context)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                f"Something went wrong: {type(context.error).__name__}. Please try again."
            )
        except Exception:
            log.exception("Could not send error message")


def build_application() -> Application:
    private = filters.ChatType.PRIVATE
    app =Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start, filters=private))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(private & (filters.VOICE | filters.AUDIO), on_voice))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    """Run locally by polling Telegram. This removes any webhook set for Vercel."""
    log.info("Bot running locally (polling) with model %s", GEMINI_MODEL)
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
