"""Claude CLI runner with stream-json parsing."""

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass
from typing import AsyncIterator

logger = logging.getLogger(__name__)


def _find_claude_path() -> str:
    """Find claude CLI binary."""
    # Check env override first
    env_path = os.environ.get("CLAUDE_PATH")
    if env_path:
        return env_path
    # Try to find in PATH
    found = shutil.which("claude")
    if found:
        return found
    # Common install locations
    home = os.path.expanduser("~")
    for candidate in [
        os.path.join(home, ".local", "bin", "claude"),
        os.path.join(home, ".claude", "bin", "claude"),
        "/usr/local/bin/claude",
    ]:
        if os.path.isfile(candidate):
            return candidate
    return "claude"  # fallback, hope it's in PATH


CLAUDE_PATH = _find_claude_path()


@dataclass
class ToolUseEvent:
    """Claude is using a tool."""
    tool: str
    input_summary: str  # short description of what's being done


@dataclass
class TextDelta:
    """Chunk of text from Claude's response."""
    text: str


@dataclass
class FinalResult:
    """Claude finished responding."""
    text: str
    session_id: str
    cost_usd: float = 0.0


@dataclass
class ErrorResult:
    """Something went wrong."""
    error: str


Event = ToolUseEvent | TextDelta | FinalResult | ErrorResult


def _summarize_tool_input(tool_name: str, tool_input: dict) -> str:
    """Create a human-readable summary of a tool call."""
    match tool_name:
        case "Read":
            path = tool_input.get("file_path", "?")
            return path.split("/")[-1] if "/" in str(path) else str(path)
        case "Edit":
            path = tool_input.get("file_path", "?")
            fname = path.split("/")[-1] if "/" in str(path) else str(path)
            old = tool_input.get("old_string", "")
            lines = old.count("\n") + 1 if old else 0
            return f"{fname} ({lines} lines)" if lines else fname
        case "Write":
            path = tool_input.get("file_path", "?")
            return path.split("/")[-1] if "/" in str(path) else str(path)
        case "Bash":
            cmd = tool_input.get("command", "?")
            return cmd[:80] + "..." if len(str(cmd)) > 80 else str(cmd)
        case "Glob":
            return tool_input.get("pattern", "?")
        case "Grep":
            pattern = tool_input.get("pattern", "?")
            path = tool_input.get("path", "")
            if path:
                return f'"{pattern}" in {path.split("/")[-1]}'
            return f'"{pattern}"'
        case _:
            return str(tool_input)[:60]


TOOL_ICONS = {
    "Read": "📖",
    "Edit": "✏️",
    "Write": "📝",
    "Bash": "⚙️",
    "Glob": "🔍",
    "Grep": "🔍",
    "WebSearch": "🌐",
    "WebFetch": "🌐",
}


class ClaudeRunner:
    """Manages a Claude CLI subprocess with stream-json output."""

    def __init__(self, claude_path: str = CLAUDE_PATH):
        self.claude_path = claude_path
        self._process: asyncio.subprocess.Process | None = None

    async def run(
        self,
        message: str,
        cwd: str,
        session_id: str | None = None,
        continue_session: bool = False,
        allowed_tools: list[str] | None = None,
        accept_edits: bool = False,
    ) -> AsyncIterator[Event]:
        """Run claude CLI and yield parsed events."""
        args = [
            self.claude_path,
            "--print",
            "--verbose",
            "--output-format", "stream-json",
        ]

        send_file_instruction = (
            "\n\nFILE SENDING: If the user asks to send, show, or share a file, "
            "use the tag <<SEND_FILE:relative/path/to/file>> in your response. "
            "The bot will automatically send that file via Telegram. "
            "You can send multiple files by using multiple tags. "
            "The path must be relative to the project root."
        )

        if accept_edits:
            args.extend(["--permission-mode", "acceptEdits"])
            args.extend(["--append-system-prompt",
                "You are in WORK mode. You can edit files. "
                "Before making changes, briefly explain what you plan to do."
                + send_file_instruction
            ])
        elif allowed_tools:
            args.extend(["--allowedTools", ",".join(allowed_tools)])
            args.extend(["--append-system-prompt",
                "You are in DISCUSS mode via a Telegram bot. "
                "You have read-only access (Read, Glob, Grep). "
                "Do NOT try to edit files — it's forbidden in this mode. "
                "Answer thoroughly and substantively — the user communicates by voice "
                "and expects a full dialogue, not one-word answers. "
                "Discuss ideas, suggest options, ask clarifying questions."
                + send_file_instruction
            ])

        if continue_session and session_id:
            args.extend(["--resume", session_id])

        # Pass message via stdin to avoid shell escaping issues
        args.append("-")  # read from stdin

        logger.info("Running claude: args=%s, cwd=%s", args, cwd)

        try:
            self._process = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=1024 * 1024,  # 1MB line buffer for large JSON
            )
            # Write message to stdin and close
            self._process.stdin.write(message.encode("utf-8"))
            await self._process.stdin.drain()
            self._process.stdin.close()

            accumulated_text = ""
            result_session_id = session_id or ""
            result_cost = 0.0

            async for line in self._process.stdout:
                line = line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ev_type = event.get("type", "")

                if ev_type == "system" and event.get("subtype") == "init":
                    sid = event.get("session_id", "")
                    if sid:
                        result_session_id = sid

                elif ev_type == "assistant":
                    # Full assistant message — extract tool_use blocks
                    msg = event.get("message", {})
                    for block in msg.get("content", []):
                        if block.get("type") == "tool_use":
                            tool = block.get("name", "?")
                            inp = block.get("input", {})
                            icon = TOOL_ICONS.get(tool, "🔧")
                            summary = _summarize_tool_input(tool, inp)
                            yield ToolUseEvent(
                                tool=f"{icon} {tool}",
                                input_summary=summary,
                            )
                        elif block.get("type") == "text":
                            text = block.get("text", "")
                            if text:
                                accumulated_text = text

                elif ev_type == "result":
                    result_session_id = event.get("session_id", result_session_id)
                    result_cost = event.get("total_cost_usd", 0.0)
                    result_text = event.get("result", accumulated_text)
                    if result_text:
                        accumulated_text = result_text

            await self._process.wait()

            stderr_output = ""
            if self._process.stderr:
                stderr_data = await self._process.stderr.read()
                stderr_output = stderr_data.decode("utf-8", errors="replace").strip()

            if stderr_output:
                logger.warning("Claude stderr: %s", stderr_output[:500])

            if self._process.returncode != 0 and not accumulated_text:
                yield ErrorResult(error=stderr_output or f"Claude exited with code {self._process.returncode}")
            else:
                yield FinalResult(
                    text=accumulated_text,
                    session_id=result_session_id,
                    cost_usd=result_cost,
                )

        except asyncio.CancelledError:
            await self.stop()
            yield ErrorResult(error="Cancelled by user")
        except Exception as e:
            logger.exception("Claude runner error")
            yield ErrorResult(error=str(e))
        finally:
            self._process = None

    async def stop(self):
        """Kill the running claude process."""
        if self._process and self._process.returncode is None:
            try:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    self._process.kill()
                    await self._process.wait()
            except ProcessLookupError:
                pass
            logger.info("Claude process stopped")

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None
