"""Sequential, bounded-memory FFmpeg rendering with per-scene checkpoints."""

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from app.argstudio.models import Episode, Settings


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    with os.fdopen(
        os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
        "w",
        encoding="utf-8",
    ) as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def command(args, cwd, timeout=3600):
    result = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )
    if result.returncode:
        raise RuntimeError("Media command failed: " + result.stderr[-1500:])
    return result.stdout


def probe(path):
    data = json.loads(
        command(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(Path(path).resolve()),
            ],
            ".",
            30,
        )
    )
    duration = float(data["format"]["duration"])
    if duration <= 0:
        raise ValueError("Media has no duration")
    return duration, data["streams"]


def validate_video(path):
    duration, streams = probe(path)
    if not {"video", "audio"} <= {s["codec_type"] for s in streams}:
        raise ValueError("Output is missing audio or video")
    command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(Path(path).resolve()),
            "-f",
            "null",
            "-",
        ],
        ".",
        3600,
    )
    return duration


def cached(folder, stage):
    manifest = folder / f"{stage}.json"
    if not manifest.exists():
        return False
    try:
        items = json.loads(manifest.read_text(encoding="utf-8"))
        return bool(items) and all(
            (folder / name).is_file() and digest(folder / name) == sha
            for name, sha in items.items()
        )
    except (ValueError, OSError):
        return False


def checkpoint(folder, stage, names):
    atomic_json(folder / f"{stage}.json", {n: digest(folder / n) for n in names})


def _stamp(seconds):
    millis = round(seconds * 1000)
    return f"{millis // 3600000:02}:{millis // 60000 % 60:02}:{millis // 1000 % 60:02},{millis % 1000:03}"


def demo_subtitles(text, duration, path):
    words = text.split()
    chunks = [" ".join(words[i : i + 6]) for i in range(0, len(words), 6)]
    path.write_text(
        "\n\n".join(
            f"{i + 1}\n{_stamp(i * duration / len(chunks))} --> {_stamp((i + 1) * duration / len(chunks))}\n{part}"
            for i, part in enumerate(chunks)
        )
        + "\n",
        encoding="utf-8",
    )


def _audio(scene, settings, folder):
    if cached(folder, "audio"):
        return probe(folder / "audio.mp3")[0]
    if settings.mode == "demo":
        command(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=24000:cl=mono",
                "-t",
                str(settings.demo_scene_seconds),
                "audio.mp3",
            ],
            folder,
        )
        demo_subtitles(
            scene.narration, settings.demo_scene_seconds, folder / "captions.srt"
        )
    else:
        # This version intentionally exposes only the free Edge adapter.
        if not re.fullmatch(
            r"[a-z]{2}-[A-Z]{2}-[A-Za-z]+Neural(?:-(?:Male|Female))?", settings.voice
        ):
            raise ValueError("ARG Studio supports Edge Neural voices only")
        from app.services import voice

        maker = voice.tts(
            text=scene.narration,
            voice_name=voice.parse_voice_name(settings.voice),
            voice_rate=1.0,
            voice_file=str(folder / "audio.mp3"),
        )
        if maker is None:
            raise RuntimeError("Edge narration failed")
        if settings.subtitles == "whisper":
            from app.services import subtitle

            subtitle.create(
                audio_file=str(folder / "audio.mp3"),
                subtitle_file=str(folder / "captions.srt"),
                word_level=True,
            )
        else:
            voice.create_subtitle(
                maker, scene.narration, str(folder / "captions.srt"), word_level=True
            )
        if (
            not (folder / "captions.srt").is_file()
            or (folder / "captions.srt").stat().st_size == 0
        ):
            raise ValueError("No synchronized captions were generated")
    duration = probe(folder / "audio.mp3")[0]
    checkpoint(folder, "audio", ["audio.mp3", "captions.srt"])
    return duration


def _visuals(scene, settings, folder, task_id, duration):
    if cached(folder, "visuals"):
        return sorted(folder.glob("visual-*"))
    if settings.mode == "demo":
        return []
    if settings.visual_provider == "local":
        sources = settings.local_visuals
    else:
        from app.models.schema import VideoAspect, VideoConcatMode
        from app.services.material import download_videos

        sources = download_videos(
            task_id=task_id,
            search_terms=scene.visual_terms,
            source=settings.visual_provider,
            video_aspect=VideoAspect.landscape,
            video_concat_mode=VideoConcatMode.sequential,
            audio_duration=duration,
            max_clip_duration=8,
        )
    if not sources:
        raise ValueError("No visual material found")
    # Isolate source snapshots, including provenance, from future user edits.
    for previous in folder.glob("visual-*"):
        previous.unlink()
    saved = []
    for i, source in enumerate(sources):
        source = Path(source).resolve()
        if source.suffix.lower() not in {
            ".mp4",
            ".mov",
            ".mkv",
            ".webm",
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
        }:
            raise ValueError("Unsupported visual format")
        target = folder / f"visual-{i:03}{source.suffix.lower()}"
        shutil.copyfile(source, target)
        saved.append(target)
    atomic_json(
        folder / "sources.json",
        {
            "provider": settings.visual_provider,
            "sources": [str(p) for p in sources],
            "material_task_id": task_id,
            "license_note": settings.asset_license_note,
            "review_required": True,
        },
    )
    checkpoint(folder, "visuals", [p.name for p in saved] + ["sources.json"])
    return saved


def _render_scene(scene, settings, folder, task_id, portrait):
    folder.mkdir(parents=True, exist_ok=True)
    if cached(folder, "render"):
        return folder / "scene.mp4"
    duration = _audio(scene, settings, folder)
    sources = _visuals(scene, settings, folder, task_id, duration)
    width, height = settings.width, settings.width * 9 // 16
    if portrait:
        width, height = height, width
    # Equal-length visual segments concatenate to the measured narration duration.
    segments = []
    for i, source in enumerate(sources or [None]):
        segment = f"visualclip-{i}.mp4"
        args = ["ffmpeg", "-v", "error", "-y", "-threads", str(settings.threads)]
        if source is None:
            args += ["-f", "lavfi", "-i", f"color=c=0x102032:s={width}x{height}:r=24"]
        elif source.suffix in {".jpg", ".jpeg", ".png", ".webp"}:
            args += ["-loop", "1", "-i", source.name]
        else:
            args += ["-stream_loop", "-1", "-i", source.name]
        args += [
            "-vf",
            f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1",
            "-t",
            str(duration / max(len(sources), 1)),
            "-an",
            "-r",
            "24",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-threads",
            str(settings.threads),
            segment,
        ]
        command(args, folder)
        segments.append(segment)
    (folder / "visuals.txt").write_text(
        "".join(f"file '{p}'\n" for p in segments), encoding="utf-8"
    )
    caption_style = (
        "FontName=DejaVu Sans,FontSize=22,Outline=2,MarginV=28"
        if portrait
        else "FontName=DejaVu Sans,FontSize=18,Outline=2,MarginV=18"
    )
    command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "1",
            "-i",
            "visuals.txt",
            "-i",
            "audio.mp3",
            "-vf",
            f"subtitles=captions.srt:force_style='{caption_style}'",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-t",
            str(duration),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "24",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-threads",
            str(settings.threads),
            "-movflags",
            "+faststart",
            "scene.part.mp4",
        ],
        folder,
    )
    validate_video(folder / "scene.part.mp4")
    (folder / "scene.part.mp4").replace(folder / "scene.mp4")
    checkpoint(folder, "render", ["scene.mp4"])
    return folder / "scene.mp4"


def render(store, job):
    ep = store.episode(job["episode_id"])
    story = Episode.model_validate_json(ep["story"])
    settings = Settings.model_validate_json(ep["settings"])
    portrait = job["kind"].startswith("short-")
    short = story.shorts[int(job["kind"].split("-")[1])] if portrait else None
    scenes = [short.scene] if short else story.scenes
    folder = (store.root / ep["id"] / job["kind"]).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    if not cached(folder, "final"):
        parts = [
            _render_scene(
                scene,
                settings,
                folder / f"scene-{i:03}",
                f"arg-{ep['id']}-{job['kind']}-{i}",
                portrait,
            )
            for i, scene in enumerate(scenes)
        ]
        (folder / "concat.txt").write_text(
            "".join(f"file '{p.parent.name}/{p.name}'\n" for p in parts),
            encoding="utf-8",
        )
        command(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                "concat.txt",
                "-c",
                "copy",
                "joined.mp4",
            ],
            folder,
        )
        audio_inputs = [
            p for p in (settings.music_file, settings.sound_effect_file) if p
        ]
        if audio_inputs:
            args = ["ffmpeg", "-v", "error", "-y", "-i", "joined.mp4"]
            for i, source in enumerate(audio_inputs):
                target = folder / f"licensed-audio-{i}{Path(source).suffix}"
                if not target.exists():
                    shutil.copyfile(source, target)
                args += ["-stream_loop", "-1", "-i", target.name]
            filters = ";".join(
                f"[{i + 1}:a]volume=0.08[a{i}]" for i in range(len(audio_inputs))
            )
            filters += (
                ";[0:a]"
                + "".join(f"[a{i}]" for i in range(len(audio_inputs)))
                + f"amix=inputs={len(audio_inputs) + 1}:duration=first:normalize=0[out]"
            )
            args += [
                "-filter_complex",
                filters,
                "-map",
                "0:v",
                "-map",
                "[out]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                "final.part.mp4",
            ]
            command(args, folder)
        else:
            shutil.copyfile(folder / "joined.mp4", folder / "final.part.mp4")
        validate_video(folder / "final.part.mp4")
        (folder / "final.part.mp4").replace(folder / "final.mp4")
        command(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                "final.mp4",
                "-frames:v",
                "1",
                "-update",
                "1",
                "thumbnail.jpg",
            ],
            folder,
        )
        checkpoint(folder, "final", ["final.mp4", "thumbnail.jpg"])
    duration = probe(folder / "final.mp4")[0]
    result = {
        "path": str(folder / "final.mp4"),
        "thumbnail": str(folder / "thumbnail.jpg"),
        "duration": duration,
        "episode_id": ep["id"],
        "series_id": ep["series_id"],
        "kind": job["kind"],
        "title": short.title if short else story.title,
        "description": (short.description if short else story.description)
        + f"\nSerie ARG de ficción. Episodio: {ep['id']}",
        "tags": story.tags,
        "demo": settings.mode == "demo",
        "license_review_required": True,
        "audio_license_note": settings.audio_license_note,
        "sha256": digest(folder / "final.mp4"),
    }
    if (
        not portrait
        and settings.mode == "live"
        and not settings.target_minutes * 48 <= duration <= settings.target_minutes * 72
    ):
        result["warning"] = (
            "Actual narration differs from target by more than 20%; review before approval"
        )
    atomic_json(folder / "metadata.json", result)
    return result
