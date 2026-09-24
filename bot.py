"""Telegram bot: turns Meera's voice/text notes into LinkedIn post drafts.

Flow: voice note -> Gemini transcription -> Gemini score (weak notes stop
here) -> Google News search for a current angle -> Gemini draft (with the voice
skill as system instruction) -> full output sent back to the same chat. Never
posts anywhere.
"""

import html
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import List, Optional

import httpx

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field
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


SCORE_THRESHOLD = 6

SCORING_PROMPT = """\
You screen raw notes from Meera Pillai, founder of Skinstinct (a Mumbai \
skincare brand), before any LinkedIn post is drafted from them. Her posts \
take one specific idea about skincare formulation, labels, testing, the \
industry, Indian climate or running the brand, and explain it with her \
reasoning and evidence.

Score how well the note below could become one of those posts, from 0 to 10:

- 9-10: a clear, specific idea with substance - a claim, mechanism, data \
point, customer question or first-hand moment - that could carry a full post \
almost as it stands.
- 6-8: one recognisable idea worth a post, even if rough or brief. Drafting \
can fill in the explanation, but the idea itself must be in the note.
- 4-5: touches a relevant topic but has no actual point yet - a topic label, \
a vague feeling, or a question with no angle.
- 1-3: an abandoned half-sentence, a fragment, or something with no \
post-worthy idea.
- 0: a logistics or task reminder (calls, orders, meetings, deliveries, \
errands), personal chatter, or unrelated to her work.

Be strict. Most quick notes are reminders or fragments. Only score 6 or above \
when you can say in one sentence what the post would argue. If in doubt, \
score lower.

Give a one-line reason, written to Meera, that names what the note has or \
is missing.

NOTE:
{note}"""


class NoteScore(BaseModel):
    score: int = Field(ge=0, le=10)
    reason: str


async def score_note(note: str) -> NoteScore:
    response = await gemini.models.generate_content(
        model=GEMINI_MODEL,
        contents=SCORING_PROMPT.format(note=note),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=NoteScore,
        ),
    )
    response_text(response, "score")  # raises on a blocked or empty reply
    if not isinstance(response.parsed, NoteScore):
        raise GeminiError("Gemini's score couldn't be read.")
    return response.parsed


# ---------------------------------------------------------------------------
# News angle
# ---------------------------------------------------------------------------

KEYWORDS_PROMPT = """\
Pull 3 to 5 search terms from the note below, then combine them into one \
short search phrase (2 to 6 words) that would find a recent news article on \
the note's core topic: an ingredient, a label claim, a regulation, a study or \
an industry practice. Use general terms a journalist would use. Leave out \
Skinstinct, Meera and any other names of people.

NOTE:
{note}"""

SUMMARY_PROMPT = """\
Write a one-line summary (under 25 words) of what this news article is \
about. You only have the headline and the publication, so describe what the \
headline says and do not add any facts, numbers or claims that are not in it.

HEADLINE: {headline}
PUBLICATION: {source}"""


class SearchTerms(BaseModel):
    keywords: List[str]
    phrase: str


@dataclass
class NewsItem:
    headline: str
    source: str
    date: str
    url: str
    summary: str = ""


async def extract_search_terms(note: str) -> SearchTerms:
    response = await gemini.models.generate_content(
        model=GEMINI_MODEL,
        contents=KEYWORDS_PROMPT.format(note=note),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SearchTerms,
        ),
    )
    response_text(response, "search terms")
    if not isinstance(response.parsed, SearchTerms):
        raise GeminiError("Gemini's search terms couldn't be read.")
    return response.parsed


async def search_google_news(query: str) -> Optional[NewsItem]:
    """Return the top Google News result for query, or None if there isn't one."""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        response = await client.get(
            "https://news.google.com/rss/search",
            params={"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"},
        )
        response.raise_for_status()
    item = ET.fromstring(response.content).find("./channel/item")
    if item is None:
        return None

    source = (item.findtext("source") or "").strip()
    headline = html.unescape((item.findtext("title") or "").strip())
    # Google appends " - Publication" to every headline.
    if source and headline.endswith(f" - {source}"):
        headline = headline[: -len(f" - {source}")]
    try:
        date = parsedate_to_datetime(item.findtext("pubDate") or "").strftime("%-d %b %Y")
    except (TypeError, ValueError):
        date = "date unknown"
    return NewsItem(
        headline=headline,
        source=source or "unknown publication",
        date=date,
        url=(item.findtext("link") or "").strip(),
    )


async def find_news(note: str) -> Optional[NewsItem]:
    terms = await extract_search_terms(note)
    log.info("News search phrase: %r, keywords: %s", terms.phrase, terms.keywords)
    # Prefer the last 30 days; widen the search if that finds nothing.
    queries = [f"{terms.phrase} when:30d", terms.phrase, " ".join(terms.keywords[:3])]
    for query in queries:
        news = await search_google_news(query)
        if news:
            break
    else:
        return None

    # The RSS feed carries only the headline, so the summary is written from it.
    response = await gemini.models.generate_content(
        model=GEMINI_MODEL,
        contents=SUMMARY_PROMPT.format(headline=news.headline, source=news.source),
    )
    try:
        news.summary = response_text(response, "summary")
    except GeminiError:
        news.summary = ""
    return news


def news_was_used(draft: str) -> bool:
    """True unless the NEWS ANGLE section clearly says the item was not used.

    If the section can't be found we assume it was used, so the verify flag is
    never left off a draft that relies on the news item.
    """
    match = re.search(r"NEWS ANGLE\W*\n(.*?)(?=^\W*CORE IDEA|\Z)", draft, re.S | re.M)
    if not match:
        return True
    angle = match.group(1).strip().lstrip("*[- ").lower()
    return not angle.startswith("not used")


def verify_flag(news: NewsItem) -> str:
    rule = "\u2500" * 32
    return (
        f"{rule}\n"
        f"NEWS SOURCE: {news.headline}\n"
        f"FROM: {news.source} \u00b7 {news.date}\n"
        f"LINK: {news.url}\n"
        "\u26a0 Check this before publishing \u2013 you are the author of this claim\n"
        f"{rule}"
    )


async def draft_post(note: str, news: Optional[NewsItem] = None) -> str:
    if news:
        angle = (
            f"CURRENT ANGLE:\n"
            f"Headline: {news.headline}\n"
            f"Publication: {news.source}\n"
            f"Date: {news.date}\n"
            f"URL: {news.url}\n"
            f"Summary (written from the headline only, not the article): {news.summary}\n\n"
            "If this news item is genuinely relevant, use it to make the post "
            "timely. If it doesn't fit naturally, ignore it. Only the headline "
            "is known, so don't state anything about the article beyond what "
            "the headline says."
        )
    else:
        angle = "CURRENT ANGLE: none provided."

    response = await gemini.models.generate_content(
        model=GEMINI_MODEL,
        contents=(
            f"NOTE:\n{note}\n\n"
            f"{angle}\n\n"
            "Write one LinkedIn post following the skill, and return "
            "all four sections in the output format it specifies."
        ),
        config=types.GenerateContentConfig(system_instruction=VOICE_SKILL),
    )
    draft = response_text(response, "draft")
    if news and news_was_used(draft):
        draft += "\n\n" + verify_flag(news)
    return draft


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
        result = await score_note(note)
    except GeminiError as e:
        await update.effective_message.reply_text(f"Scoring failed: {e}")
        return
    except genai_errors.APIError as e:
        log.exception("Gemini API error while scoring")
        await update.effective_message.reply_text(
            f"Scoring failed: the Gemini API returned an error ({e.code} {e.status}). "
            "Try sending the note again in a minute."
        )
        return

    log.info("Note scored %s/10: %s", result.score, result.reason)
    if result.score < SCORE_THRESHOLD:
        await update.effective_message.reply_text(
            f"No draft made. This note scored {result.score}/10 "
            f"(a draft needs {SCORE_THRESHOLD} or more).\n\n{result.reason}"
        )
        return

    # A failed news search shouldn't cost Meera her draft: say so and carry on.
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        news = await find_news(note)
        news_line = (
            f"News angle found: {news.headline} ({news.source}, {news.date})"
            if news
            else "No matching news found, so the draft won't have a news angle."
        )
    except (GeminiError, genai_errors.APIError, httpx.HTTPError, ET.ParseError) as e:
        log.exception("News search failed")
        news = None
        news_line = f"News search failed ({type(e).__name__}), so the draft won't have a news angle."

    await update.effective_message.reply_text(
        f"Note scored {result.score}/10: {result.reason}\n\n{news_line}\n\nWriting the draft now..."
    )

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        draft = await draft_post(note, news)
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
