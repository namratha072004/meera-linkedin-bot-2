# Meera LinkedIn draft bot

A Telegram bot that turns Meera Pillai's voice notes and text notes into LinkedIn post drafts in her voice, and sends the drafts back in the same chat for review. It never posts to LinkedIn.

## How it works

1. You send the bot a voice note or a text note in a private chat.
2. A voice note is transcribed with Gemini, and the bot replies with the transcript first so you can spot any mishearing.
3. The note goes to Gemini with `meera_voice_skill.md` as the system instruction.
4. The bot replies with Gemini's full output: DRAFT, CHECK BEFORE POSTING, NEWS ANGLE and CORE IDEA.
5. If the reply is longer than Telegram's 4,096-character limit, it is split into several messages. Whole sections are kept together when they fit. A section that is too long on its own is split between paragraphs, or between words if it has to be.

Anyone who finds the bot in Telegram can use it, and every note they send is billed to your Gemini API key. Keep the bot's username private. The bot only answers in private chats, not in groups.

## Setup

You need Python 3.9 or newer.

### 1. Create the bot in Telegram

1. Open a chat with [@BotFather](https://t.me/BotFather) and send `/newbot`.
2. Choose a name and username. BotFather replies with a token like `123456:ABC-...`.

### 2. Get a Gemini API key

Go to https://aistudio.google.com/apikey and create a key. The same key is used for transcription and drafting.

### 3. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Configure

```bash
cp .env.example .env
```

Open `.env` and fill in both values:

```
TELEGRAM_BOT_TOKEN=123456:ABC-...
GEMINI_API_KEY=...
```

Optional settings:

- `GEMINI_MODEL`: defaults to `gemini-3.8-flash`. It's used for both transcription and drafting.
- `VOICE_SKILL_PATH`: defaults to `meera_voice_skill.md` next to `bot.py`. Edit that file to change how drafts are written. Restart the bot after editing it.

### 5. Run

```bash
python bot.py
```

Open your bot in Telegram, send `/start`, then send a note. The bot runs for as long as the terminal is open. Stop it with Ctrl+C.

## Errors

If a step fails, the bot replies with a plain message saying which step failed, for example:

- `Couldn't download the voice note from Telegram. Please send it again.`
- `Transcription failed: the Gemini API returned an error (400 INVALID_ARGUMENT). ...`
- `Drafting failed: the Gemini API returned an error (429 RESOURCE_EXHAUSTED). ...`
- `Drafting failed: Gemini blocked the draft (SAFETY).`

The full error details are printed in the terminal where `bot.py` is running.

If the bot starts and then stops straight away, read the terminal. It tells you which `.env` value is missing or which file it couldn't find.

## Notes

- A draft takes roughly 20-60 seconds. While it's working, Telegram shows "typing…".
- Replies are sent as plain text, so the section headings appear as `**DRAFT**` and so on, exactly as Gemini wrote them.
- Every note is sent with `CURRENT ANGLE: none provided`, so NEWS ANGLE will normally read "Not used".
