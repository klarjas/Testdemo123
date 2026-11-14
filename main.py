from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from pipeline import PipelineError, dub_video
from settings import (
    BOT_TOKEN,
    MAX_INPUT_BYTES,
    MAX_OUTPUT_BYTES,
    OPENAI_API_KEY,
    OPENAI_TIMEOUT_SECONDS,
    PORT,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("khmer-auto-dub")

api = FastAPI(
    title="Khmer Auto-Dub Telegram Bot",
    docs_url=None,
    redoc_url=None,
)
BOT_STATUS = "starting"
USER_LOCKS: dict[int, asyncio.Lock] = {}


@api.get("/")
async def root() -> dict[str, str]:
    return {
        "service": "Khmer Auto-Dub Telegram Bot",
        "status": BOT_STATUS,
        "health": "/health",
    }


@api.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "khmer-auto-dub-telegram-bot",
        "bot": BOT_STATUS,
    }


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        "សួស្តី! នេះគឺជា Khmer Auto-Dub Bot 🎬\n\n"
        "សូមផ្ញើវីដេអូជាឯកសារ Video មកខ្ញុំ។ "
        "Bot នឹងស្តាប់សំឡេងដើម បម្លែងទៅជាអត្ថបទ បកប្រែជាខ្មែរ "
        "បែងចែកតួនាទីសំឡេង និងបង្កើតវីដេអូ Dubbing ថ្មី។\n\n"
        "កំណត់ចំណាំ៖ វីដេអូត្រូវតែមានសំឡេង និងមានទំហំមិនលើស 20 MB "
        "តាម Telegram Bot API សាធារណៈ។"
    )


async def handle_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    media = message.video or message.document
    if media is None:
        await message.reply_text("សូមផ្ញើឯកសារវីដេអូ។")
        return

    if message.document:
        mime_type = message.document.mime_type or ""
        if not mime_type.startswith("video/"):
            await message.reply_text("ឯកសារនេះមិនមែនជា Video ទេ។")
            return

    file_size = getattr(media, "file_size", None)
    if file_size is not None and file_size > MAX_INPUT_BYTES:
        await message.reply_text(
            "វីដេអូធំពេក។ សូមផ្ញើវីដេអូដែលមានទំហំមិនលើស 20 MB "
            "ឬកាត់បន្ថយទំហំមុនផ្ញើ។"
        )
        return

    lock = USER_LOCKS.setdefault(user.id, asyncio.Lock())
    if lock.locked():
        await message.reply_text("ការងារមុនរបស់អ្នកកំពុងដំណើរការ។ សូមរង់ចាំឱ្យចប់សិន។")
        return

    await lock.acquire()
    job_dir = Path(tempfile.mkdtemp(prefix=f"khmer_dub_{user.id}_"))
    status_message = None

    async def progress(text: str) -> None:
        if status_message is None:
            return
        try:
            await status_message.edit_text(text)
        except Exception:
            logger.debug("Telegram progress edit failed", exc_info=True)

    try:
        status_message = await message.reply_text("កំពុងទទួលវីដេអូ…")
        original_name = getattr(media, "file_name", None) or "input_video.mp4"
        suffix = Path(original_name).suffix.lower()
        allowed_suffixes = {
            ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpeg", ".mpg"
        }
        if suffix not in allowed_suffixes:
            suffix = ".mp4"

        input_path = job_dir / f"source{suffix}"
        output_path = job_dir / "khmer_dubbed.mp4"

        telegram_file = await context.bot.get_file(media.file_id)
        await telegram_file.download_to_drive(custom_path=str(input_path))

        actual_size = input_path.stat().st_size if input_path.exists() else 0
        if actual_size == 0:
            raise PipelineError("Telegram downloaded an empty file.")
        if actual_size > MAX_INPUT_BYTES:
            raise PipelineError("The downloaded video exceeds the 20 MB input limit.")

        client: AsyncOpenAI = context.application.bot_data["openai_client"]
        result = await dub_video(
            input_path=input_path,
            output_path=output_path,
            work_dir=job_dir / "work",
            client=client,
            progress=progress,
        )

        output_size = output_path.stat().st_size
        if output_size > MAX_OUTPUT_BYTES:
            await progress(
                "ការបង្កើតបានជោគជ័យ ប៉ុន្តែ MP4 ធំពេកសម្រាប់ការផ្ញើតាម Telegram។ "
                "សូមប្រើវីដេអូដើមដែលមាន bitrate ទាបជាងនេះ។"
            )
            return

        await progress("រួចរាល់! កំពុងផ្ញើវីដេអូ Dubbing ខ្មែរ…")
        with output_path.open("rb") as video_file:
            await message.reply_video(
                video=video_file,
                caption=(
                    "✅ Khmer Auto-Dub រួចរាល់\n"
                    f"⏱ រយៈពេល៖ {result['duration_seconds']:.1f} វិនាទី\n"
                    f"💬 ចំនួនបន្ទាត់៖ {result['segment_count']}"
                ),
                supports_streaming=True,
                read_timeout=300,
                write_timeout=300,
                connect_timeout=60,
                pool_timeout=60,
            )
        try:
            await status_message.delete()
        except Exception:
            pass

    except PipelineError as exc:
        logger.warning("Dubbing job rejected or failed: %s", exc)
        await progress(f"មិនអាចបញ្ចប់ការងារបានទេ៖ {exc}")
    except Exception as exc:
        logger.exception("Unexpected error while processing a video")
        await progress(
            "មានបញ្ហាបច្ចេកទេសពេលដំណើរការ។ "
            f"ប្រភេទកំហុស៖ {type(exc).__name__}។ សូមពិនិត្យ Render logs ឬ local console។"
        )
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
        lock.release()
        if USER_LOCKS.get(user.id) is lock:
            USER_LOCKS.pop(user.id, None)


async def telegram_error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    logger.error("Unhandled Telegram update error", exc_info=context.error)


async def run() -> None:
    global BOT_STATUS

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Add it to .env or Render environment variables.")
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY is missing. Add it to .env or Render environment variables."
        )

    client = AsyncOpenAI(
        api_key=OPENAI_API_KEY,
        timeout=OPENAI_TIMEOUT_SECONDS,
        max_retries=2,
    )

    application = Application.builder().token(BOT_TOKEN).build()
    application.bot_data["openai_client"] = client
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(
        MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video)
    )
    application.add_error_handler(telegram_error_handler)

    await application.initialize()
    await application.start()
    if application.updater is None:
        raise RuntimeError("Telegram polling updater is unavailable.")

    await application.updater.start_polling(
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )
    BOT_STATUS = "running"
    logger.info("Telegram polling started; FastAPI health server will use port %s.", PORT)

    server = uvicorn.Server(
        uvicorn.Config(
            api,
            host="0.0.0.0",
            port=PORT,
            log_level="info",
            access_log=False,
        )
    )

    try:
        await server.serve()
    finally:
        BOT_STATUS = "stopping"
        if application.updater and application.updater.running:
            await application.updater.stop()
        await application.stop()
        await application.shutdown()
        await client.close()
        BOT_STATUS = "stopped"


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Shutdown requested.")
