# Khmer Auto-Dub Telegram Bot

A Telegram video-to-Khmer-dubbing bot. The same codebase runs on Windows (Python 3.11/3.13) and Render (Docker, Python 3.11).

## Features

1. **Ingest and extract audio** — accepts Telegram video messages and video documents, then uses FFmpeg to extract mono 16 kHz audio.
2. **Transcribe with timestamps** — uses OpenAI Whisper (whisper-1, verbose_json, segment timestamps). Audio is split into 8-minute MP3 chunks to stay comfortably below the transcription upload limit.
3. **Translate and tag roles** — uses gpt-4o-mini with JSON-object output to produce natural spoken Khmer and one of five roles: narrator, male, old_male, female, or child.
4. **Synthesize and mix** — uses Edge TTS Khmer voices, builds a timestamped voice timeline with pydub, ducks the original soundtrack to 20%, and renders an MP4 with FFmpeg.

No PyTorch, CUDA, local speech model, or GPU is used.

Python 3.13 installs audioop-lts as a compatibility module for pydub because the standard-library audioop module was removed in Python 3.13. Python 3.11 uses its built-in audioop.

## Requirements

- Python 3.11 or 3.13
- FFmpeg and FFprobe available on PATH (installed in the Docker image)
- A Telegram bot token from BotFather
- An OpenAI API key
- Internet access for OpenAI APIs and Edge TTS

**Important:** Edge TTS is an online speech service; the dubbing process is not fully offline. OpenAI transcription and translation also require an internet connection and API usage.

## Run on Windows

1. Install Python 3.11 or 3.13 and FFmpeg. Make sure ffmpeg and ffprobe work in Command Prompt.
2. Download or clone this repository.
3. Double-click start_windows.bat.
4. On the first run, the script creates .env from .env.example. Enter your values:
   - BOT_TOKEN — Telegram bot token
   - OPENAI_API_KEY — OpenAI API key
   - PORT — default 10000
   - MAX_VIDEO_SECONDS — default 2700 (45 minutes)
5. Run start_windows.bat again.

You can also run it manually:

~~~powershell
py -3.13 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env
python main.py
~~~

## Deploy on Render

1. Create a **Web Service** and connect this GitHub repository.
2. Choose **Docker** as the runtime. Render will build from the included Dockerfile.
3. Add these environment variables in Render:
   - BOT_TOKEN
   - OPENAI_API_KEY
   - PORT (Render supplies this automatically; if you set it manually, use the value Render assigns)
   - MAX_VIDEO_SECONDS (optional; default 2700)
4. Deploy. Render health check path: /health.

Do not commit .env, bot tokens, or API keys. .env.example contains blank values only.

## Telegram file limits

The standard hosted Telegram Bot API limits bot downloads through getFile to 20 MB. This project rejects larger incoming files with a clear message. The outgoing video is kept below 49 MB for the standard hosted Bot API upload limit.

## Runtime and memory safety

- No heavyweight PyTorch or CUDA packages.
- Whisper audio is split into short MP3 chunks rather than uploading one enormous WAV.
- The pydub voice timeline is mono, 16 kHz, 16-bit PCM.
- The default maximum video length is 45 minutes to limit memory use on a 512 MB service. Change MAX_VIDEO_SECONDS only after considering available memory.
- Every job runs in a temporary directory that is removed in a finally block, including failed jobs.

## Voice profile mapping

| Role | Voice | Pitch |
|---|---|---:|
| narrator | km-KH-PisethNeural | +0 Hz |
| male | km-KH-PisethNeural | +4 Hz |
| old_male | km-KH-PisethNeural | -14 Hz |
| female | km-KH-SreymomNeural | +0 Hz |
| child | km-KH-SreymomNeural | +15 Hz |

If a synthesized line exceeds its source segment duration, the bot retries with a faster rate, capped at +35%. If it still does not fit, the clip is trimmed to the source segment window.

**Speaker-role note:** Whisper segment timestamps are used for alignment. The GPT role labels are contextual estimates based on the dialogue and nearby text; this configuration does not perform acoustic speaker diarization or verify a character's real voice from audio.

## Health endpoint

- GET / — service identity and current bot status
- GET /health — Render-compatible health response

## Troubleshooting

- ffmpeg or ffprobe not found: install FFmpeg and add its bin directory to Windows PATH.
- Telegram rejects a large video: reduce the source video size; the public Bot API download cap is 20 MB.
- Missing credentials: set BOT_TOKEN and OPENAI_API_KEY in .env locally or Render environment variables in production.
- Edge voice unavailable: confirm internet connectivity and that the requested Khmer Edge TTS voice is available.
- Render out of memory: lower MAX_VIDEO_SECONDS and use shorter/smaller source videos.
