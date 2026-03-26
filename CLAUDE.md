# Voice Claude Bot

Telegram bot providing voice and text interface to Claude Code CLI.

## Project structure
- `bot/main.py` — entry point, Telegram application setup, owner-only filter
- `bot/handlers.py` — command handlers (/start, /go, /discuss, /stop, etc.) and message processing
- `bot/claude_runner.py` — Claude CLI subprocess manager, stream-json parser
- `bot/session.py` — project/session state management, persistence to `data/state.json`
- `bot/stt.py` — speech-to-text via Groq Whisper API (with CJK hallucination filter)
- `data/` — runtime data (state.json, tmp audio files), gitignored except .gitkeep

## Key concepts
- **Two modes**: discuss (read-only tools) and work (accept edits, auto-returns to discuss)
- **Session persistence**: Claude Code session IDs stored per-project, supports resume
- **Stream parsing**: Claude CLI runs with `--output-format stream-json`, events parsed line-by-line
- **File sending**: Claude can include `<<SEND_FILE:path>>` tags, bot sends files to Telegram
- **Owner filter**: bot only responds to TELEGRAM_OWNER_CHAT_ID

## Running
```bash
cp .env.example .env  # fill in tokens
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python -m bot.main
```

## Dependencies
- Python 3.11+
- Claude Code CLI (installed and authenticated)
- ffmpeg (voice message conversion)
- External APIs: Telegram Bot API, Groq Whisper API
