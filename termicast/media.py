"""Media preparation: FFmpeg audio presets and Pillow image presets.

All conversion streams to temporary files on the same filesystem as the
destination and atomically installs complete, validated outputs. Source files
are never modified. Conversion requires FFmpeg; copy/pass-through paths and
image processing do not.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
import io
import json
import os
from pathlib import Path
import shutil
import subprocess

from .storage import asset_root


AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
TRANSCRIPT_EXTENSIONS = {".vtt"}

# RSS-deliverable enclosure formats used by keep-audio and feed generation.
ENCLOSURE_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
}

AUDIO_PRESETS = {
    "standard": {"bitrate": 128_000, "sample_rate": 44100, "codec": "libmp3lame"},
    "music": {"bitrate": 192_000, "sample_rate": 44100, "codec": "libmp3lame"},
}

IMAGE_PRESETS = {
    "compact": {"target_bytes": 500 * 1024, "qualities": (85, 80, 75, 70)},
    "detail": {"target_bytes": 1024 * 1024, "qualities": (90, 85, 80)},
}

MAX_IMAGE_EDGE = 3000

# Shared MIME map for enclosures, artwork, transcripts, chapters, and the feed.
_OTHER_TYPES = {
    ".vtt": "text/vtt",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".json": "application/json",
    ".srt": "text/plain", ".txt": "text/plain",
    ".html": "text/html", ".pdf": "application/pdf",
}


def content_type_for(relative_path):
    """Return the Content-Type for a managed relative path, or None if unknown."""
    if relative_path == "feed.xml":
        return "application/rss+xml"
    if relative_path.endswith(".json") and relative_path.startswith("chapters/"):
        return "application/json+chapters"
    suffix = Path(relative_path).suffix.lower()
    if suffix in ENCLOSURE_TYPES:
        return ENCLOSURE_TYPES[suffix]
    return _OTHER_TYPES.get(suffix)


@dataclass
class Prepared:
    audio_path: Path
    audio_relative: str
    audio_before: int
    audio_after: int
    audio_meta: dict
    audio_note: str = ""
    image_path: Path | None = None
    image_relative: str | None = None
    image_before: int | None = None
    image_after: int | None = None
    image_note: str = ""
    image_dims: tuple[int, int] | None = None
    transcript_path: Path | None = None
    transcript_relative: str | None = None
    warnings: list = field(default_factory=list)

    def review(self) -> dict:
        data = {
            "audio_file": self.audio_relative,
            "audio_before": f"{self.audio_before} bytes",
            "audio_after": f"{self.audio_after} bytes",
            "audio_codec": self.audio_meta.get("codec", "?"),
            "audio_sample_rate": f"{self.audio_meta.get('sample_rate', '?')} Hz",
            "audio_channels": self.audio_meta.get("channels", "?"),
            "audio_duration": f"{self.audio_meta.get('duration', '?')} s",
        }
        if self.image_relative:
            data["image_file"] = self.image_relative
            data["image_before"] = f"{self.image_before} bytes"
            data["image_after"] = f"{self.image_after} bytes"
            if self.image_dims:
                data["image_dimensions"] = f"{self.image_dims[0]}x{self.image_dims[1]}"
        if self.transcript_relative:
            data["transcript_file"] = self.transcript_relative
        return data


@contextmanager
def _progress(enabled, description):
    import sys
    if not enabled or not sys.stdout.isatty():
        yield lambda done, total=None: None
        return
    from rich.progress import Progress, TextColumn, BarColumn
    with Progress(TextColumn("[progress.description]{task.description}"),
                  BarColumn(), TextColumn("{task.percentage:>3.0f}%")) as progress:
        task = progress.add_task(description, total=None)

        def update(done, total=None):
            if total:
                progress.update(task, total=total)
            progress.update(task, completed=done)

        yield update


def identify_files(paths):
    """Classify supplied paths by role; reject missing or ambiguous inputs."""
    roles = {"audio": [], "image": [], "transcript": []}
    for path in paths:
        suffix = Path(path).suffix.lower()
        if suffix in AUDIO_EXTENSIONS:
            roles["audio"].append(str(path))
        elif suffix in IMAGE_EXTENSIONS:
            roles["image"].append(str(path))
        elif suffix in TRANSCRIPT_EXTENSIONS:
            roles["transcript"].append(str(path))
        else:
            raise ValueError(f"Unsupported file type for {path}: expected audio, image, or .vtt transcript")
    if not roles["audio"]:
        raise ValueError("Audio is required for a new episode")
    if len(roles["audio"]) > 1:
        raise ValueError("Exactly one audio file may be supplied")
    if len(roles["image"]) > 1:
        raise ValueError("Exactly one cover image may be supplied")
    if len(roles["transcript"]) > 1:
        raise ValueError("Exactly one transcript may be supplied")
    return {key: (value[0] if value else None) for key, value in roles.items()}


def _require_ffmpeg():
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RuntimeError(
            "FFmpeg is required for audio preparation. Install it with "
            "'sudo apt install ffmpeg', then retry. Existing publication, "
            "help, and deployment of already-prepared files remain available.")


def probe_audio(path):
    """Return codec, sample rate, channels, duration, and bitrate via ffprobe."""
    process = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration,bit_rate:stream=codec_name,codec_type,sample_rate,channels,bit_rate",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60, check=True, stdin=subprocess.DEVNULL)
    data = json.loads(process.stdout)
    result = {"codec": "", "sample_rate": 0, "channels": 0, "duration": 0.0, "bitrate": 0}
    fmt = data.get("format", {})
    try:
        result["duration"] = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        result["duration"] = 0.0
    try:
        result["bitrate"] = int(fmt.get("bit_rate") or 0)
    except (TypeError, ValueError):
        result["bitrate"] = 0
    for stream in data.get("streams", []):
        if stream.get("codec_type") != "audio":
            continue
        result["codec"] = stream.get("codec_name", "")
        try:
            result["sample_rate"] = int(stream.get("sample_rate") or 0)
        except (TypeError, ValueError):
            pass
        try:
            result["channels"] = int(stream.get("channels") or 0)
        except (TypeError, ValueError):
            pass
        try:
            stream_bitrate = int(stream.get("bit_rate") or 0)
        except (TypeError, ValueError):
            stream_bitrate = 0
        if stream_bitrate:
            result["bitrate"] = stream_bitrate
        break
    return result


def _atomic_install(temp_path, final_path):
    from .publisher import fsync_dir
    final_path = Path(final_path)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temp_path, final_path)
    fsync_dir(final_path.parent)


def _copy_stream(source, destination, update=None):
    source = Path(source)
    total = source.stat().st_size
    copied = 0
    with source.open("rb") as src, destination.open("wb") as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
            copied += len(chunk)
            if update:
                update(copied, total or None)
    return copied


def _ffmpeg(args, update=None, duration=None):
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, stdin=subprocess.DEVNULL)
    for line in process.stdout:
        line = line.strip()
        if line.startswith("out_time_us="):
            try:
                done = int(line.split("=", 1)[1]) / 1_000_000
                if update:
                    update(done, duration or None)
            except (ValueError, IndexError):
                pass
    process.wait()
    if process.returncode != 0:
        stderr = process.stderr.read().strip()
        raise RuntimeError(f"FFmpeg failed: {stderr or process.returncode}")


def _prepare_audio(source, dest_dir, slug, preset_name, keep, update=None):
    source = Path(source)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    before = source.stat().st_size
    suffix = source.suffix.lower()
    if keep:
        if suffix not in ENCLOSURE_TYPES:
            raise ValueError(
                "Keep audio requires an RSS-deliverable format (mp3, m4a, aac, ogg, opus, wav, flac); "
                f"{suffix} is not publishable as an enclosure")
        meta = probe_audio(source)
        _check_channels(meta)
        dest = dest_dir / (slug + suffix)
        temp = dest_dir / f".{dest.name}.tmp-{os.getpid()}"
        try:
            _copy_stream(source, temp, update)
            _atomic_install(temp, dest)
        finally:
            temp.unlink(missing_ok=True)
        return dest, f"audio/{dest.name}", before, dest.stat().st_size, meta, "kept original audio"

    preset = AUDIO_PRESETS[preset_name]
    meta = probe_audio(source)
    _check_channels(meta)
    pass_through = (suffix == ".mp3" and meta["sample_rate"] == 44100
                    and (meta["bitrate"] == 0 or meta["bitrate"] <= preset["bitrate"]))
    dest = dest_dir / (slug + ".mp3")
    temp = dest_dir / f".{dest.name}.tmp-{os.getpid()}"
    try:
        if pass_through:
            _copy_stream(source, temp, update)
            note = "passed through (already suitable MP3)"
        else:
            _require_ffmpeg()
            args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats",
                    "-progress", "pipe:1", "-i", str(source), "-vn",
                    "-c:a", preset["codec"], "-b:a", str(preset["bitrate"]),
                    "-ar", str(preset["sample_rate"]), "-f", "mp3", str(temp)]
            _ffmpeg(args, update=update, duration=meta["duration"] or None)
            note = f"converted to MP3 {preset['bitrate'] // 1000} kbps / {preset['sample_rate']} Hz"
        _atomic_install(temp, dest)
        meta = probe_audio(dest)
    finally:
        temp.unlink(missing_ok=True)
    return dest, f"audio/{dest.name}", before, dest.stat().st_size, meta, note


def _check_channels(meta):
    if meta["channels"] > 2:
        raise ValueError(
            "Multichannel audio is not supported. Export a mono or stereo file "
            f"and retry (detected {meta['channels']} channels).")


def _needs_orientation(source):
    from PIL import Image
    try:
        with Image.open(source) as image:
            return image.getexif().get(0x0112, 1) not in (1, None)
    except Exception:
        return True


def _prepare_image(source, dest_dir, slug, preset_name, keep, update=None):
    from PIL import Image, ImageOps
    from .publisher import atomic_write
    source = Path(source)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    before = source.stat().st_size
    suffix = source.suffix.lower()

    with Image.open(source) as image:
        fmt = image.format
        size = image.size
        image.verify()

    if keep:
        if fmt not in ("JPEG", "PNG"):
            raise ValueError("Keep image requires JPEG or PNG artwork")
        ext = ".jpg" if suffix in (".jpg", ".jpeg") else ".png"
        dest = dest_dir / (slug + ext)
        temp = dest_dir / f".{dest.name}.tmp-{os.getpid()}"
        try:
            _copy_stream(source, temp, update)
            _atomic_install(temp, dest)
        finally:
            temp.unlink(missing_ok=True)
        return dest, f"images/{dest.name}", before, dest.stat().st_size, size, "kept original artwork"

    preset = IMAGE_PRESETS[preset_name]
    target = preset["target_bytes"]
    fast_path = (fmt == "JPEG" and suffix in (".jpg", ".jpeg")
                 and before <= target and not _needs_orientation(source)
                 and max(size) <= MAX_IMAGE_EDGE)
    if fast_path:
        dest = dest_dir / (slug + ".jpg")
        temp = dest_dir / f".{dest.name}.tmp-{os.getpid()}"
        try:
            _copy_stream(source, temp, update)
            _atomic_install(temp, dest)
        finally:
            temp.unlink(missing_ok=True)
        return dest, f"images/{dest.name}", before, dest.stat().st_size, size, "preserved suitable JPEG"

    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image)
        width, height = image.size
        if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
            background = Image.new("RGBA", image.size, (255, 255, 255, 255))
            image = Image.alpha_composite(background, image.convert("RGBA")).convert("RGB")
            flattened = True
        else:
            image = image.convert("RGB")
            flattened = False
        if width == height and width > MAX_IMAGE_EDGE:
            image = image.resize((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.LANCZOS)
            width = height = MAX_IMAGE_EDGE
            resized = True
        else:
            resized = False
        final = None
        for quality in preset["qualities"]:
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=quality, optimize=True)
            final = buffer.getvalue()
            if len(final) <= target:
                break
    dest = dest_dir / (slug + ".jpg")
    atomic_write(dest, final)
    notes = []
    if flattened:
        notes.append("transparency flattened onto white")
    if resized:
        notes.append(f"resized to {width}x{height}")
    if not notes:
        notes.append(f"optimized JPEG (target below {target} bytes)")
    note = "; ".join(notes)
    return dest, f"images/{dest.name}", before, dest.stat().st_size, (width, height), note


def _prepare_transcript(source, dest_dir, slug, update=None):
    from .assets import check_vtt
    from .publisher import atomic_write
    source = Path(source)
    before = source.stat().st_size
    text = source.read_text(encoding="utf-8-sig")
    check_vtt(text)
    dest = dest_dir / (slug + ".vtt")
    atomic_write(dest, text.encode("utf-8"))
    return dest, f"transcripts/{dest.name}", before, dest.stat().st_size


def prepare_media(show, files, *, slug, audio_preset=None, image_preset=None,
                  keep_audio=False, keep_image=False, progress=False) -> Prepared:
    """Prepare audio/image/transcript into the show's asset root.

    `files` is the dict from identify_files() with keys audio/image/transcript.
    Each file is prepared once, one at a time, and validated before install.
    """
    audio_preset = audio_preset or show.get("audio_preset", "standard")
    image_preset = image_preset or show.get("image_preset", "compact")
    if audio_preset not in AUDIO_PRESETS:
        raise ValueError("audio_preset must be standard or music")
    if image_preset not in IMAGE_PRESETS:
        raise ValueError("image_preset must be compact or detail")

    root = asset_root(show)
    audio_dir = root / "audio"
    image_dir = root / "images"
    transcript_dir = root / "transcripts"
    for directory in (audio_dir, image_dir, transcript_dir):
        directory.mkdir(parents=True, exist_ok=True)

    prepared = Prepared(audio_path=Path(), audio_relative="", audio_before=0, audio_after=0, audio_meta={})
    with _progress(progress, f"Preparing {slug}") as update:
        prepared.audio_path, prepared.audio_relative, prepared.audio_before, prepared.audio_after, \
            prepared.audio_meta, prepared.audio_note = _prepare_audio(
                files["audio"], audio_dir, slug, audio_preset, keep_audio, update)
        if files.get("image"):
            prepared.image_path, prepared.image_relative, prepared.image_before, prepared.image_after, \
                prepared.image_dims, prepared.image_note = _prepare_image(
                    files["image"], image_dir, slug, image_preset, keep_image, update)
        if files.get("transcript"):
            prepared.transcript_path, prepared.transcript_relative, _, _ = _prepare_transcript(
                files["transcript"], transcript_dir, slug, update)
    return prepared
