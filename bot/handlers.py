"""Telegram message handlers and command handlers."""

import asyncio
import logging
import os
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ChatAction, ParseMode

from . import stt
from .claude_runner import ClaudeRunner, ToolUseEvent, TextDelta, FinalResult, ErrorResult
from .session import (
    PROJECTS, UserState, load_state, save_state,
)

logger = logging.getLogger(__name__)

DISCUSS_TOOLS = ["Read", "Glob", "Grep", "WebSearch", "WebFetch"]

# Shared state
_state: UserState | None = None
_runner = ClaudeRunner()
_claude_task: asyncio.Task | None = None


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
    global _claude_task
    if _runner.is_running:
        await _runner.stop()
        if _claude_task and not _claude_task.done():
            _claude_task.cancel()
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
    voice = "🔊 on" if state.voice_response else "🔇 off"

    await update.message.reply_text(
        f"Project: {state.project_name}\n"
        f"📂 {state.project_path}\n"
        f"Mode: {mode}\n"
        f"Claude: {running}\n"
        f"Voice: {voice}"
    )


async def cmd_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle voice response."""
    state = get_state()
    args = (update.message.text or "").split()
    if len(args) > 1:
        state.voice_response = args[1].lower() in ("on", "1", "yes")
    else:
        state.voice_response = not state.voice_response
    _save()
    status = "🔊 on" if state.voice_response else "🔇 off"
    await update.message.reply_text(f"Voice: {status}")


# ── Project selection ─────────────────────────────────────────────────────


async def _show_project_selection(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = []
    for key, proj in PROJECTS.items():
        keyboard.append([InlineKeyboardButton(proj["name"], callback_data=f"project:{key}")])
    keyboard.append([InlineKeyboardButton("📁 Custom path...", callback_data="project:custom")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Select project:", reply_markup=reply_markup)


async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard callbacks."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("project:"):
        project_key = data.split(":", 1)[1]
        if project_key == "custom":
            await query.edit_message_text(
                "Send the full path to the project directory:"
            )
            return

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

        text = await stt.transcribe(groq_key, mp3_path)

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

    except Exception as e:
        logger.exception("Voice processing error")
        await update.message.reply_text(f"❌ Error: {e}")


async def _process_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Send message to Claude and stream results."""
    global _claude_task

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

                # Auto-return to discuss mode after work
                if accept_edits:
                    state.set_discuss_mode()
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

            if accept_edits:
                await update.message.reply_text("💬 Mode returned: discuss")

    except Exception as e:
        logger.exception("Claude processing error")
        await update.message.reply_text(f"❌ Error: {e}")


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
