from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
PORT = int(os.getenv("PORT", "10000"))
MAX_VIDEO_SECONDS = int(os.getenv("MAX_VIDEO_SECONDS", "2700"))

# Telegram's hosted Bot API has a 20 MB getFile download limit.
MAX_INPUT_BYTES = 20 * 1024 * 1024

# Keep below Telegram's standard hosted Bot API upload limit.
MAX_OUTPUT_BYTES = 49 * 1024 * 1024

WHISPER_CHUNK_SECONDS = 8 * 60
OPENAI_TIMEOUT_SECONDS = 300

if not 1 <= PORT <= 65535:
    raise ValueError("PORT must be between 1 and 65535.")

if MAX_VIDEO_SECONDS < 60:
    raise ValueError("MAX_VIDEO_SECONDS must be at least 60.")
