# Meera LinkedIn draft bot

A Telegram bot that turns Meera Pillai's voice notes and text notes into LinkedIn post drafts in her voice, and sends the drafts back in the same chat for review. It never posts to LinkedIn.

## How it works

1. You send the bot a voice note or a text note in a private chat.
2. A voice note is transcribed with Gemini, and the bot replies with the transcript first so you can spot any mishearing.
3. Gemini scores the note from 0 to 10 on whether it holds one idea worth a post, with a one-line reason. Task reminders and abandoned half-sentences score low; a specific claim, data point, customer question or first-hand moment scores high.
4. If the score is below 6, the bot replies with the score and the reason, and no draft is made. The cutoff is `SCORE_THRESHOLD` in `bot.py`, and the scoring rules are `SCORING_PROMPT` just above it.
5. If the score is 6 or above, the bot replies with the score, then sends the note to Gemini with `meera_voice_skill.md` as the system instruction.
6. The bot replies with Gemini's full output: DRAFT, CHECK BEFORE POSTING, NEWS ANGLE and CORE IDEA.
7. If the reply is longer than Telegram's 4,096-character limit, it is split into several messages. Whole sections are kept together when they fit. A section that is too long on its own is split between paragraphs, or between words if it has to be.

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

Open `.env` and fill in the values:

```
TELEGRAM_BOT_TOKEN=123456:ABC-...
GEMINI_API_KEY=...
TELEGRAM_WEBHOOK_SECRET=...
```

`TELEGRAM_WEBHOOK_SECRET` is only needed for Vercel. It can be any long random string. Generate one with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Optional settings:

- `GEMINI_MODEL`: defaults to `gemini-3.8-flash`. It's used for both transcription and drafting.
- `VOICE_SKILL_PATH`: defaults to `meera_voice_skill.md` next to `bot.py`. Edit that file to change how drafts are written. Restart the bot after editing it.

### 5. Run it

There are two ways to run the bot. Use one or the other, not both at once.

**On your computer.** This is good for testing:

```bash
python bot.py
```

The bot runs for as long as the terminal is open. Stop it with Ctrl+C. Starting it this way turns off the Vercel webhook, so run `set_webhook.py` again (step 3 below) when you go back to Vercel.

**On Vercel.** This runs all the time, with no computer on:

1. Import the GitHub repository at https://vercel.com/new. Vercel finds `app.py` and `vercel.json` on its own, so leave the build settings as they are.
2. Under **Settings → Environment Variables**, add `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY` and `TELEGRAM_WEBHOOK_SECRET`, using the same values as your `.env`. Then redeploy from the **Deployments** tab so the new values are picked up.
3. Tell Telegram where to send messages. Use your deployment's URL:

   ```bash
   python set_webhook.py https://your-project.vercel.app
   ```

Opening `https://your-project.vercel.app` in a browser should show "Meera draft bot is running."

Then open your bot in Telegram, send `/start`, and send a note.

## Errors

If a step fails, the bot replies with a plain message saying which step failed, for example:

- `Couldn't download the voice note from Telegram. Please send it again.`
- `Transcription failed: the Gemini API returned an error (400 INVALID_ARGUMENT). ...`
- `Drafting failed: the Gemini API returned an error (429 RESOURCE_EXHAUSTED). ...`
- `Drafting failed: Gemini blocked the draft (SAFETY).`

The full error details are printed in the terminal where `bot.py` is running. On Vercel, they're under the project's **Logs** tab.

If the bot starts and then stops straight away, read the terminal. It tells you which `.env` value is missing or which file it couldn't find.

If the bot doesn't answer on Vercel, run `python set_webhook.py https://your-project.vercel.app` again. It prints the last delivery error Telegram saw, for example a 403, which means `TELEGRAM_WEBHOOK_SECRET` on Vercel doesn't match your `.env`.

## Notes

- A draft takes roughly 20-60 seconds. While it's working, Telegram shows "typing…".
- Replies are sent as plain text, so the section headings appear as `**DRAFT**` and so on, exactly as Gemini wrote them.
- Every note is sent with `CURRENT ANGLE: none provided`, so NEWS ANGLE will normally read "Not used".
