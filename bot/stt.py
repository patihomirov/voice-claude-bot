"""Speech-to-text via Groq Whisper API."""

import asyncio
import logging
import os
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


async def convert_to_mp3(input_path: str) -> str:
    """Convert audio file to MP3 using ffmpeg."""
    output_path = input_path.rsplit(".", 1)[0] + ".mp3"
    if output_path == input_path:
        output_path = input_path + ".mp3"

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", input_path,
        "-vn", "-c:a", "libmp3lame", "-q:a", "4", "-y",
        output_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        logger.warning("ffmpeg failed: %s, using original", stderr.decode(errors="replace"))
        return input_path

    return output_path


async def transcribe(api_key: str, file_path: str, language: str = "ru") -> str | None:
    """Transcribe audio file via Groq Whisper API."""
    ext = Path(file_path).suffix.lower()
    mime_types = {
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".oga": "audio/ogg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".webm": "audio/webm",
    }
    content_type = mime_types.get(ext, "application/octet-stream")

    async with httpx.AsyncClient(timeout=300) as client:
        with open(file_path, "rb") as f:
            resp = await client.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (f"audio{ext}", f, content_type)},
                data={
                    "model": "whisper-large-v3-turbo",
                    "language": language,
                    "response_format": "verbose_json",
                },
            )

    if resp.status_code != 200:
        logger.error("Groq error %d: %s", resp.status_code, resp.text)
        return None

    result = resp.json()

    # Filter hallucinated segments
    segments = result.get("segments", [])
    if segments:
        parts = []
        lang = result.get("language", language)
        for s in segments:
            if s.get("no_speech_prob", 0) > 0.5:
                continue
            text = s.get("text", "").strip()
            if text and not _is_cjk_hallucination(text, lang):
                parts.append(text)
        if parts:
            return " ".join(parts)

    return (result.get("text") or "").strip() or None


def _is_cjk_hallucination(text: str, lang: str) -> bool:
    """Check if text contains CJK characters in a non-CJK language."""
    if lang in ("ja", "japanese", "zh", "chinese", "ko", "korean"):
        return False
    return any(
        0x4E00 <= ord(c) <= 0x9FFF
        or 0x3040 <= ord(c) <= 0x309F
        or 0x30A0 <= ord(c) <= 0x30FF
        for c in text
    )
