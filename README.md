# Voice Claude Bot

A Telegram bot that provides voice and text interface to [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI. Send voice messages or text — the bot transcribes speech, forwards it to Claude Code working on your project, and streams back the results.

## Features

- **Voice input** — send Telegram voice messages, transcribed via Groq Whisper API
- **File & photo input** — send documents (code, text, configs) or photos/screenshots for Claude to analyze
- **Multi-project** — switch between projects with inline keyboard
- **Two modes**:
  - **Discuss** (default) — Claude has read-only access (Read, Glob, Grep, WebSearch)
  - **Work** — Claude can edit files, auto-returns to discuss after each response
- **Session persistence** — resume previous Claude sessions or start new ones
- **File sending** — Claude can send project files to you via `<<SEND_FILE:path>>` tags
- **Tool activity feed** — see what tools Claude is using in real-time
- **Owner-only** — only responds to the configured Telegram user ID
- **IPv4 forced** — avoids Telegram API IPv6 timeout issues

## Architecture

```
Telegram Voice/Text
        │
        ▼
  bot/handlers.py  ←→  bot/session.py (state management)
        │
        ├─→ bot/stt.py (Groq Whisper transcription)
        │
        └─→ bot/claude_runner.py (Claude CLI subprocess)
                │
                ▼
         claude --print --verbose --output-format stream-json
```

## Requirements

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- ffmpeg (for voice message conversion)
- Telegram bot token (from [@BotFather](https://t.me/BotFather))
- Groq API key (for speech-to-text, free tier available at [console.groq.com](https://console.groq.com))

## Setup

```bash
# Clone
git clone https://github.com/YOUR_USERNAME/voice-claude-bot.git
cd voice-claude-bot

# Create virtualenv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your tokens

# Configure projects (either edit session.py defaults or set PROJECTS_JSON in .env)

# Run
./start.sh
```

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Select a project |
| `/project` | Switch project |
| `/new` | New Claude session |
| `/go` | Work mode (allow edits) |
| `/discuss` | Discuss mode (read-only) |
| `/stop` | Stop running Claude process |
| `/status` | Current status |

## How it works

1. You send a voice message, text, document, or photo
2. Voice messages are downloaded, converted to MP3, and transcribed via Groq Whisper
3. Documents: text files are included inline in the prompt; binary files and photos are saved and Claude reads them via the Read tool
4. The prompt is sent to Claude Code CLI as a subprocess with `--print --verbose --output-format stream-json`
5. Tool usage events are streamed back as status updates
6. The final response is split into Telegram-friendly chunks and sent back
7. In work mode, Claude auto-returns to discuss mode after responding

## License

MIT
