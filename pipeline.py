from __future__ import annotations

import asyncio
import json
import logging
import math
from pathlib import Path
from typing import Any, Awaitable, Callable

import edge_tts
from openai import AsyncOpenAI
from pydub import AudioSegment

from settings import MAX_VIDEO_SECONDS, WHISPER_CHUNK_SECONDS

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], Awaitable[None]]

ROLE_PROFILES: dict[str, tuple[str, str]] = {
    "narrator": ("km-KH-PisethNeural", "+0Hz"),
    "male": ("km-KH-PisethNeural", "+4Hz"),
    "old_male": ("km-KH-PisethNeural", "-14Hz"),
    "female": ("km-KH-SreymomNeural", "+0Hz"),
    "child": ("km-KH-SreymomNeural", "+15Hz"),
}

VALID_ROLES = set(ROLE_PROFILES)


class PipelineError(RuntimeError):
    """A user-safe error raised by the dubbing pipeline."""


async def notify(progress: ProgressCallback | None, text: str) -> None:
    if progress is None:
        return
    try:
        await progress(text)
    except Exception:
        logger.debug("Could not update Telegram progress message", exc_info=True)


async def run_command(*args: str, timeout: float = 1800) -> tuple[bytes, bytes]:
    """Run an external program without blocking the asyncio event loop."""
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise PipelineError(f"External command timed out after {timeout:.0f} seconds.") from exc

    if process.returncode != 0:
        details = stderr.decode("utf-8", errors="replace")[-2500:].strip()
        raise PipelineError(
            f"Command failed (exit {process.returncode}). "
            f"{details or 'No diagnostic output was returned.'}"
        )
    return stdout, stderr


def read_field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def normalize_role(value: Any) -> str:
    candidate = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "oldmale": "old_male",
        "elderly_male": "old_male",
        "older_male": "old_male",
        "woman": "female",
        "girl": "female",
        "boy": "child",
        "young_child": "child",
        "voiceover": "narrator",
        "voice_over": "narrator",
    }
    candidate = aliases.get(candidate, candidate)
    return candidate if candidate in VALID_ROLES else "male"


async def inspect_media(path: Path) -> tuple[float, bool]:
    stdout, _ = await run_command(
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration:stream=codec_type",
        "-of", "json",
        str(path),
        timeout=60,
    )
    try:
        data = json.loads(stdout.decode("utf-8"))
        duration = float(data["format"]["duration"])
        has_audio = any(
            stream.get("codec_type") == "audio"
            for stream in data.get("streams", [])
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PipelineError("Could not read the video's duration or stream information.") from exc

    if duration <= 0:
        raise PipelineError("The video has an invalid duration.")
    if duration > MAX_VIDEO_SECONDS:
        minutes = MAX_VIDEO_SECONDS // 60
        raise PipelineError(
            f"This project is configured for videos up to {minutes} minutes. "
            "Increase MAX_VIDEO_SECONDS only when the Render memory limit allows it."
        )
    if not has_audio:
        raise PipelineError("The uploaded video does not contain an audio track.")
    return duration, has_audio


async def extract_and_split_audio(input_path: Path, work_dir: Path) -> list[Path]:
    wav_path = work_dir / "source_mono_16khz.wav"
    await run_command(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(input_path),
        "-map", "0:a:0",
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(wav_path),
        timeout=1800,
    )

    pattern = work_dir / "whisper_chunk_%04d.mp3"
    await run_command(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(wav_path),
        "-map", "0:a:0",
        "-f", "segment",
        "-segment_time", str(WHISPER_CHUNK_SECONDS),
        "-reset_timestamps", "1",
        "-c:a", "libmp3lame",
        "-b:a", "64k",
        "-ac", "1",
        "-ar", "16000",
        str(pattern),
        timeout=1800,
    )

    chunks = sorted(work_dir.glob("whisper_chunk_*.mp3"))
    if not chunks:
        raise PipelineError("FFmpeg extracted audio but did not create Whisper chunks.")
    return chunks


async def transcribe_chunks(
    chunks: list[Path],
    client: AsyncOpenAI,
    progress: ProgressCallback | None,
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    next_id = 0

    for chunk_index, chunk_path in enumerate(chunks):
        await notify(
            progress,
            f"២/៤ កំពុងស្តាប់សំឡេង និងកំណត់ពេលវេលា ({chunk_index + 1}/{len(chunks)})…",
        )
        with chunk_path.open("rb") as audio_file:
            result = await client.audio.transcriptions.create(
                file=audio_file,
                model="whisper-1",
                response_format="verbose_json",
                timestamp_granularities=["segment"],
            )

        offset = chunk_index * WHISPER_CHUNK_SECONDS
        raw_segments = read_field(result, "segments", []) or []
        for raw in raw_segments:
            text = str(read_field(raw, "text", "") or "").strip()
            if not text:
                continue
            try:
                start = max(0.0, float(read_field(raw, "start", 0.0)) + offset)
                end = max(start + 0.15, float(read_field(raw, "end", start + 0.2)) + offset)
            except (TypeError, ValueError):
                continue

            collected.append({
                "id": next_id,
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
            })
            next_id += 1

    collected.sort(key=lambda item: (item["start"], item["id"]))
    if not collected:
        raise PipelineError(
            "Whisper did not detect any dialogue. Try a video with clearer speech."
        )
    return collected


def context_lines(segments: list[dict[str, Any]]) -> list[str]:
    return [
        str(item.get("text", "")).strip()
        for item in segments
        if str(item.get("text", "")).strip()
    ]


async def translate_and_tag(
    segments: list[dict[str, Any]],
    client: AsyncOpenAI,
    progress: ProgressCallback | None,
) -> list[dict[str, Any]]:
    translated: list[dict[str, Any]] = []
    batch_size = 30

    system_prompt = """
You are a professional Khmer movie dubbing translator and dialogue role tagger.
Translate every supplied line into natural, spoken Cambodian Khmer suitable for voice acting.
Keep the original meaning, emotion, names, relationships, and conversational context.
Do not summarize, omit, merge, or add dialogue. Keep each segment ID unchanged.
Classify each line into exactly one role:
- narrator: off-screen narration or documentary/story voice-over
- male: adult male character
- old_male: older-sounding male character
- female: adult female character
- child: child character
Use context to keep a character's role consistent across nearby lines.
Choose the most likely role from dialogue context; use male when evidence is insufficient.
Return only a valid JSON object with this exact shape:
{"segments":[{"id":0,"khmer":"Khmer dialogue","role":"male"}]}
The role must be one of: narrator, male, old_male, female, child.
Do not return timestamps or any extra fields.
""".strip()

    for batch_start in range(0, len(segments), batch_size):
        batch = segments[batch_start:batch_start + batch_size]
        batch_end = batch_start + len(batch)
        before = context_lines(segments[max(0, batch_start - 3):batch_start])
        after = context_lines(segments[batch_end:batch_end + 2])
        payload = {
            "context_before": before,
            "segments": [
                {"id": item["id"], "text": item["text"]}
                for item in batch
            ],
            "context_after": after,
        }

        await notify(
            progress,
            f"៣/៤ កំពុងបកប្រែជាខ្មែរ និងកំណត់តួនាទីសំឡេង "
            f"({batch_end}/{len(segments)} បន្ទាត់)…",
        )
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            temperature=0.2,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
        )

        content = response.choices[0].message.content or ""
        try:
            data = json.loads(content)
            rows = data.get("segments", [])
        except (json.JSONDecodeError, AttributeError, TypeError) as exc:
            raise PipelineError("GPT translation returned invalid JSON. Please retry.") from exc

        if not isinstance(rows, list):
            raise PipelineError("GPT translation returned an unexpected segment list.")

        by_id: dict[int, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                row_id = int(row.get("id"))
            except (TypeError, ValueError):
                continue
            by_id[row_id] = row

        for source in batch:
            row = by_id.get(int(source["id"]))
            if row is None:
                raise PipelineError(
                    f"GPT translation omitted segment {source['id']}. Please retry."
                )
            khmer = str(row.get("khmer") or row.get("translation") or "").strip()
            if not khmer:
                raise PipelineError(
                    f"GPT returned empty Khmer dialogue for segment {source['id']}."
                )
            translated.append({
                **source,
                "khmer": khmer,
                "role": normalize_role(row.get("role")),
            })

    return translated


async def synthesize_segment(
    text: str,
    role: str,
    target_ms: int,
    output_path: Path,
) -> AudioSegment:
    voice, pitch = ROLE_PROFILES[normalize_role(role)]

    async def generate(rate_percent: int) -> AudioSegment:
        rate = f"+{rate_percent}%"
        communicator = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=rate,
            pitch=pitch,
        )
        await asyncio.wait_for(communicator.save(str(output_path)), timeout=180)
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise PipelineError("Khmer voice synthesis returned an empty audio clip.")
        clip = await asyncio.to_thread(
            AudioSegment.from_file,
            str(output_path),
            format="mp3",
        )
        return (
            clip
            .set_frame_rate(16000)
            .set_channels(1)
            .set_sample_width(2)
        )

    clip = await generate(0)
    if target_ms > 0 and len(clip) > target_ms:
        required_rate = math.ceil(((len(clip) / target_ms) - 1.0) * 100 + 2)
        clip = await generate(min(35, max(1, required_rate)))

    if target_ms > 0 and len(clip) > target_ms:
        clip = clip[:target_ms]

    if len(clip) > 40:
        clip = clip.fade_out(min(40, max(10, len(clip) // 8)))
    return clip


async def build_voice_timeline(
    segments: list[dict[str, Any]],
    duration_seconds: float,
    work_dir: Path,
    progress: ProgressCallback | None,
) -> Path:
    timeline_ms = max(1, int(math.ceil(duration_seconds * 1000)))
    timeline = AudioSegment.silent(duration=timeline_ms, frame_rate=16000)
    timeline = timeline.set_channels(1).set_sample_width(2)
    clip_path = work_dir / "current_voice.mp3"
    usable = [item for item in segments if str(item.get("khmer", "")).strip()]

    for index, item in enumerate(usable, start=1):
        start_ms = max(0, int(float(item["start"]) * 1000))
        end_ms = max(start_ms + 250, int(float(item["end"]) * 1000))
        target_ms = end_ms - start_ms

        clip = await synthesize_segment(
            text=str(item["khmer"]),
            role=str(item.get("role", "male")),
            target_ms=target_ms,
            output_path=clip_path,
        )
        if start_ms < timeline_ms:
            timeline = await asyncio.to_thread(
                timeline.overlay,
                clip,
                position=start_ms,
            )

        if index % 8 == 0 or index == len(usable):
            await notify(
                progress,
                f"៤/៤ កំពុងបង្កើតសំឡេងខ្មែរ និងតម្រៀបតាម timeline "
                f"({index}/{len(usable)} បន្ទាត់)…",
            )

    if not usable:
        raise PipelineError("There are no translated lines to synthesize.")

    voice_path = work_dir / "khmer_voice_timeline.wav"
    exported = await asyncio.to_thread(timeline.export, str(voice_path), format="wav")
    exported.close()
    return voice_path


async def render_dubbed_video(
    input_path: Path,
    voice_path: Path,
    output_path: Path,
    duration_seconds: float,
) -> None:
    duration_arg = f"{duration_seconds:.3f}"
    common = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(input_path),
        "-i", str(voice_path),
        "-filter_complex",
        "[0:a:0]volume=0.20[background];"
        "[background][1:a:0]amix=inputs=2:duration=first:"
        "dropout_transition=0:normalize=0[mixed]",
        "-map", "0:v:0",
        "-map", "[mixed]",
        "-c:a", "aac",
        "-b:a", "160k",
        "-t", duration_arg,
        "-movflags", "+faststart",
    ]

    try:
        await run_command(
            *common,
            "-c:v", "copy",
            str(output_path),
            timeout=1800,
        )
    except PipelineError:
        logger.warning("Stream-copy render failed; retrying with H.264 video encoding.")
        await run_command(
            *common,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "24",
            "-pix_fmt", "yuv420p",
            str(output_path),
            timeout=2400,
        )

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise PipelineError("FFmpeg did not produce the final MP4 file.")


async def dub_video(
    input_path: Path,
    output_path: Path,
    work_dir: Path,
    client: AsyncOpenAI,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)

    await notify(progress, "១/៤ កំពុងពិនិត្យវីដេអូ និងដកសំឡេងដើមជា mono 16 kHz…")
    duration, _ = await inspect_media(input_path)
    chunks = await extract_and_split_audio(input_path, work_dir)

    await notify(progress, "២/៤ កំពុងបម្លែងសំឡេងទៅជាអត្ថបទដោយ Whisper…")
    source_segments = await transcribe_chunks(chunks, client, progress)

    await notify(progress, "៣/៤ កំពុងបកប្រែ និងកំណត់តួនាទីសំឡេង…")
    translated_segments = await translate_and_tag(source_segments, client, progress)

    await notify(progress, "៤/៤ កំពុងបង្កើតសំឡេងខ្មែរ និងលាយជាមួយសំឡេងដើម 20%…")
    voice_path = await build_voice_timeline(
        translated_segments,
        duration,
        work_dir,
        progress,
    )
    await render_dubbed_video(input_path, voice_path, output_path, duration)

    return {
        "duration_seconds": round(duration, 2),
        "segment_count": len(translated_segments),
    }
