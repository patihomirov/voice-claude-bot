"""Telegram message handlers and command handlers."""

import asyncio
import logging
import os
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from . import stt
from .claude_runner import ClaudeRunner, ToolUseEvent, FinalResult, ErrorResult
from .session import (
    PROJECTS, UserState, load_state, save_state,
)

logger = logging.getLogger(__name__)

DISCUSS_TOOLS = ["Read", "Glob", "Grep", "WebSearch", "WebFetch"]

# Shared state
_state: UserState | None = None
_runner = ClaudeRunner()
_owner_id: int | None = None


def set_owner_id(owner_id: int) -> None:
    """Set the owner ID for callback query filtering."""
    global _owner_id
    _owner_id = owner_id


def get_state() -> UserState:
    global _state
    if _state is None:
        _state = load_state()
    return _state


def _save():
    save_state(get_state())


# ── Commands ──────────────────────────────────────────────────────────────


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show project selection."""
    await _show_project_selection(update, ctx)


async def cmd_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch project."""
    await _show_project_selection(update, ctx)


async def cmd_projects(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """List projects."""
    await _show_project_selection(update, ctx)


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Start new session in current project."""
    state = get_state()
    if not state.current_project:
        await update.message.reply_text("No project selected. /project")
        return
    session = state.new_session()
    _save()
    await update.message.reply_text(
        f"🆕 New session in {state.project_name}\n"
        f"Mode: discuss\n"
        f"Send text or voice message."
    )


async def cmd_go(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch to work mode (allow edits)."""
    state = get_state()
    if not state.current_project:
        await update.message.reply_text("No project selected. /project")
        return
    state.set_work_mode()
    _save()
    await update.message.reply_text(
        f"⚡ Mode: work\n"
        f"Claude can edit files.\n"
        f"To return to discuss mode: /discuss"
    )


async def cmd_discuss(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch back to discuss mode."""
    state = get_state()
    state.set_discuss_mode()
    _save()
    await update.message.reply_text("💬 Mode: discuss (read-only)")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop running Claude process."""
    if _runner.is_running:
        await _runner.stop()
        await update.message.reply_text("🛑 Stopped")
    else:
        await update.message.reply_text("Claude is not running")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current state."""
    state = get_state()
    if not state.current_project:
        await update.message.reply_text("No project selected. /project")
        return

    session = state.get_session()
    mode = "⚡ work" if state.is_work_mode else "💬 discuss"
    running = "▶️ yes" if _runner.is_running else "⏹ no"

    await update.message.reply_text(
        f"Project: {state.project_name}\n"
        f"📂 {state.project_path}\n"
        f"Mode: {mode}\n"
        f"Claude: {running}"
    )


# ── Project selection ─────────────────────────────────────────────────────


async def _show_project_selection(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = []
    for key, proj in PROJECTS.items():
        keyboard.append([InlineKeyboardButton(proj["name"], callback_data=f"project:{key}")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Select project:", reply_markup=reply_markup)


async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard callbacks."""
    query = update.callback_query

    # Owner-only check for callbacks
    if _owner_id and query.from_user.id != _owner_id:
        await query.answer("Access denied", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data.startswith("project:"):
        project_key = data.split(":", 1)[1]

        state = get_state()
        state.set_project(project_key)

        session = state.get_session()
        if session:
            # Existing session — offer continue or new
            keyboard = [
                [
                    InlineKeyboardButton("▶️ Continue", callback_data=f"session:continue:{project_key}"),
                    InlineKeyboardButton("🆕 New session", callback_data=f"session:new:{project_key}"),
                ]
            ]
            await query.edit_message_text(
                f"Project: {state.project_name}\n"
                f"📂 {state.project_path}\n\n"
                f"Previous session found.\n",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        else:
            session = state.ensure_session()
            _save()
            await query.edit_message_text(
                f"Project: {state.project_name}\n"
                f"📂 {state.project_path}\n\n"
                f"🆕 New session. Mode: discuss.\n"
                f"Send text or voice message.\n"
                f"/go — allow edits"
            )

    elif data.startswith("session:"):
        parts = data.split(":")
        action = parts[1]
        project_key = parts[2]

        state = get_state()
        state.set_project(project_key)

        if action == "new":
            state.new_session()

        _save()
        mode = "discuss"
        await query.edit_message_text(
            f"Project: {state.project_name}\n"
            f"📂 {state.project_path}\n\n"
            f"{'▶️ Session restored' if action == 'continue' else '🆕 New session'}. "
            f"Mode: {mode}.\n"
            f"Send text or voice message.\n"
            f"/go — allow edits"
        )


# ── Message handling ──────────────────────────────────────────────────────


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text messages — send to Claude."""
    text = update.message.text
    if not text:
        return
    await _process_message(update, ctx, text)


async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice messages — transcribe then send to Claude."""
    state = get_state()
    groq_key = os.environ.get("GROQ_API_KEY")
    if not groq_key:
        await update.message.reply_text("❌ GROQ_API_KEY not configured")
        return

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        await update.message.reply_text("❌ Bot token not configured")
        return

    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    # Get file info
    voice = update.message.voice
    file = await ctx.bot.get_file(voice.file_id)

    # Download using python-telegram-bot (uses configured IPv4 transport)
    tmp_dir = os.path.join(os.path.dirname(__file__), "..", "data", "tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    try:
        ext = os.path.splitext(file.file_path)[1] or ".oga"
        local_path = os.path.join(tmp_dir, f"voice_{update.message.message_id}{ext}")
        await file.download_to_drive(local_path)
        mp3_path = await stt.convert_to_mp3(local_path)

        stt_lang = os.environ.get("STT_LANGUAGE", "ru")
        text = await stt.transcribe(groq_key, mp3_path, language=stt_lang)

        # Cleanup
        for p in {local_path, mp3_path}:
            try:
                os.remove(p)
            except OSError:
                pass

        if not text:
            await update.message.reply_text("Could not transcribe voice message.")
            return

        # Show transcription
        await update.message.reply_text(f"🎤 {text}")

        # Process as text
        await _process_message(update, ctx, text)

    except Exception:
        logger.exception("Voice processing error")
        await update.message.reply_text("❌ Voice processing failed. Check bot logs.")


async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle document messages — download and send to Claude with caption."""
    doc = update.message.document
    caption = update.message.caption or ""

    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    tmp_dir = os.path.join(os.path.dirname(__file__), "..", "data", "tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    try:
        file = await ctx.bot.get_file(doc.file_id)
        filename = doc.file_name or f"file_{update.message.message_id}"
        local_path = os.path.join(tmp_dir, filename)
        await file.download_to_drive(local_path)

        # Try to read as text for inline inclusion
        text_content = _try_read_text(local_path)

        if text_content is not None:
            prompt = f"File: {filename}\n```\n{text_content}\n```"
            if caption:
                prompt += f"\n\n{caption}"
            else:
                prompt += "\n\nAnalyze this file."
            # Clean up — content is inline
            _safe_remove(local_path)
        else:
            # Binary file — save and give Claude the path
            prompt = f"File saved at: {os.path.abspath(local_path)}\nFilename: {filename}"
            if caption:
                prompt += f"\n\n{caption}"
            else:
                prompt += "\n\nAnalyze this file."

        await update.message.reply_text(f"📎 {filename}")
        await _process_message(update, ctx, prompt)

    except Exception:
        logger.exception("Document processing error")
        await update.message.reply_text("❌ Document processing failed. Check bot logs.")


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo messages — download and send to Claude for vision analysis."""
    caption = update.message.caption or ""

    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    tmp_dir = os.path.join(os.path.dirname(__file__), "..", "data", "tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    try:
        # Get the largest photo
        photo = update.message.photo[-1]
        file = await ctx.bot.get_file(photo.file_id)

        ext = os.path.splitext(file.file_path)[1] or ".jpg"
        filename = f"photo_{update.message.message_id}{ext}"
        local_path = os.path.join(tmp_dir, filename)
        await file.download_to_drive(local_path)

        prompt = f"Image saved at: {os.path.abspath(local_path)}\nUse the Read tool to view this image."
        if caption:
            prompt += f"\n\n{caption}"
        else:
            prompt += "\n\nDescribe and analyze this image."

        await update.message.reply_text(f"📷 {filename}")
        await _process_message(update, ctx, prompt)

    except Exception:
        logger.exception("Photo processing error")
        await update.message.reply_text("❌ Photo processing failed. Check bot logs.")


TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".rb",
    ".java", ".kt", ".c", ".cpp", ".h", ".hpp", ".cs", ".swift", ".sh", ".bash",
    ".zsh", ".fish", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf",
    ".json", ".xml", ".html", ".css", ".scss", ".sql", ".r", ".lua",
    ".pl", ".pm", ".php", ".ex", ".exs", ".erl", ".hs", ".ml", ".vim",
    ".dockerfile", ".makefile", ".cmake", ".gradle", ".env", ".gitignore",
    ".editorconfig", ".csv", ".tsv", ".log", ".diff", ".patch",
}

MAX_INLINE_SIZE = 100_000  # 100KB — larger files passed by path


def _try_read_text(path: str) -> str | None:
    """Try to read file as UTF-8 text. Return None if binary or too large."""
    ext = os.path.splitext(path)[1].lower()
    basename = os.path.basename(path).lower()

    # Check by extension or known filenames
    is_likely_text = (
        ext in TEXT_EXTENSIONS
        or basename in ("makefile", "dockerfile", "vagrantfile", "rakefile", "gemfile")
    )

    if not is_likely_text:
        return None

    try:
        size = os.path.getsize(path)
        if size > MAX_INLINE_SIZE:
            return None
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except (UnicodeDecodeError, OSError):
        return None


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


async def _process_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Send message to Claude and stream results."""
    state = get_state()

    if not state.current_project:
        await _show_project_selection(update, ctx)
        return

    if _runner.is_running:
        await update.message.reply_text("⏳ Claude is still running. /stop to interrupt.")
        return

    session = state.ensure_session()
    _save()

    # Determine mode
    accept_edits = state.is_work_mode
    allowed_tools = None if accept_edits else DISCUSS_TOOLS

    # Continue only if we have a real session_id from a previous Claude response
    has_session = bool(session.session_id)

    # Status message
    mode_text = "⚡ work" if accept_edits else "💬 discuss"
    status_msg = await update.message.reply_text(f"🤖 [{mode_text}] Thinking...")

    # Collect tool events and final text
    tool_lines = []
    final_text = ""
    last_update_len = 0

    async def update_status():
        """Periodically update the status message with tool events."""
        nonlocal last_update_len
        content = "\n".join(tool_lines) if tool_lines else "🤖 Thinking..."
        if len(content) != last_update_len:
            last_update_len = len(content)
            try:
                await status_msg.edit_text(content)
            except Exception:
                pass

    try:
        async for event in _runner.run(
            message=text,
            cwd=state.project_path,
            session_id=session.session_id if has_session else None,
            continue_session=has_session,
            allowed_tools=allowed_tools,
            accept_edits=accept_edits,
        ):
            if isinstance(event, ToolUseEvent):
                tool_lines.append(f"{event.tool}: {event.input_summary}")
                await update_status()

            elif isinstance(event, FinalResult):
                final_text = event.text
                session.session_id = event.session_id
                _save()


            elif isinstance(event, ErrorResult):
                final_text = f"❌ {event.error}"

        # Send final response
        if tool_lines:
            tools_summary = "\n".join(tool_lines)
            try:
                await status_msg.edit_text(tools_summary)
            except Exception:
                pass
        else:
            try:
                await status_msg.delete()
            except Exception:
                pass

        if final_text:
            # Extract and send files
            file_paths = _extract_file_paths(final_text)
            clean_text = _remove_file_tags(final_text)

            # Split long messages (Telegram limit 4096)
            if clean_text.strip():
                for chunk in _split_message(clean_text, 4000):
                    await update.message.reply_text(chunk)

            # Send files
            for rel_path in file_paths:
                await _send_file(update, state.project_path, rel_path)

    except Exception:
        logger.exception("Claude processing error")
        await update.message.reply_text("❌ Claude processing failed. Check bot logs.")


_FILE_TAG_RE = re.compile(r"<<SEND_FILE:(.+?)>>")


def _extract_file_paths(text: str) -> list[str]:
    """Extract file paths from <<SEND_FILE:path>> tags."""
    return _FILE_TAG_RE.findall(text)


def _remove_file_tags(text: str) -> str:
    """Remove <<SEND_FILE:path>> tags from text."""
    return _FILE_TAG_RE.sub("", text).strip()


async def _send_file(update: Update, project_path: str, rel_path: str) -> None:
    """Send a file from the project directory to the user."""
    full_path = os.path.join(project_path, rel_path)

    if not os.path.isfile(full_path):
        await update.message.reply_text(f"⚠️ File not found: {rel_path}")
        return

    # Security: prevent path traversal
    real_project = os.path.realpath(project_path)
    real_file = os.path.realpath(full_path)
    if not real_file.startswith(real_project):
        await update.message.reply_text(f"⚠️ Access denied: {rel_path}")
        return

    try:
        with open(full_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=os.path.basename(full_path),
            )
    except Exception as e:
        logger.error("Failed to send file %s: %s", full_path, e)
        await update.message.reply_text(f"⚠️ Error sending: {rel_path}")


def _split_message(text: str, max_len: int = 4000) -> list[str]:
    """Split text into chunks respecting line boundaries."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break

        # Find last newline before max_len
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len

        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    return chunks
