"""Offline record validation and optional, bounded remote inspection.

Validators return errors, not remote-inspection warnings. Call inspect_artwork
separately when online; unavailable metadata should allow manual entry.
"""

from datetime import datetime, timezone as dt_timezone
from io import BytesIO
import json
import math
import os
from pathlib import Path
import shutil
import re
import subprocess
import tempfile
import time
from urllib import request
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# https://podcasters.apple.com/support/1691-apple-podcasts-categories
CATEGORIES = {
    "Arts": ["Books", "Design", "Fashion & Beauty", "Food", "Performing Arts", "Visual Arts"],
    "Business": ["Careers", "Entrepreneurship", "Investing", "Management", "Marketing", "Non-Profit"],
    "Comedy": ["Comedy Interviews", "Improv", "Stand-Up"],
    "Education": ["Courses", "How To", "Language Learning", "Self-Improvement"],
    "Fiction": ["Comedy Fiction", "Drama", "Science Fiction"],
    "Government": [],
    "History": [],
    "Health & Fitness": ["Alternative Health", "Fitness", "Medicine", "Mental Health", "Nutrition", "Sexuality"],
    "Kids & Family": ["Education for Kids", "Parenting", "Pets & Animals", "Stories for Kids"],
    "Leisure": ["Animation & Manga", "Automotive", "Aviation", "Crafts", "Games", "Hobbies", "Home & Garden", "Video Games"],
    "Music": ["Music Commentary", "Music History", "Music Interviews"],
    "News": ["Business News", "Daily News", "Entertainment News", "News Commentary", "Politics", "Sports News", "Tech News"],
    "Religion & Spirituality": ["Buddhism", "Christianity", "Hinduism", "Islam", "Judaism", "Religion", "Spirituality"],
    "Science": ["Astronomy", "Chemistry", "Earth Sciences", "Life Sciences", "Mathematics", "Natural Sciences", "Nature", "Physics", "Social Sciences"],
    "Society & Culture": ["Documentary", "Personal Journals", "Philosophy", "Places & Travel", "Relationships"],
    "Sports": ["Baseball", "Basketball", "Cricket", "Fantasy Sports", "Football", "Golf", "Hockey", "Rugby", "Running", "Soccer", "Swimming", "Tennis", "Volleyball", "Wilderness", "Wrestling"],
    "Technology": [],
    "True Crime": [],
    "TV & Film": ["After Shows", "Film History", "Film Interviews", "Film Reviews", "TV Reviews"],
}

NETWORK_TIMEOUT = float(os.environ.get("TERMICAST_STALL_TIMEOUT", "30"))
DOWNLOAD_TIMEOUT = float(os.environ.get("TERMICAST_DOWNLOAD_TIMEOUT", "3600"))
MAX_ARTWORK_BYTES = 20 * 1024 * 1024
MAX_MEDIA_BYTES = int(os.environ.get("TERMICAST_MAX_MEDIA_BYTES", str(2 * 1024**3)))


def validate_https(url) -> bool:
    """Return whether URL is absolute HTTPS with a host and no credentials.

    Reject whitespace, control characters, backslashes and invalid ports. This
    is syntax validation, not a reachability check or private-network filter.
    """
    if not isinstance(url, str) or not url or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url) or "\\" in url:
        return False
    try:
        parts = urlsplit(url)
        return (parts.scheme == "https" and bool(parts.hostname)
                and parts.username is None and parts.password is None
                and (parts.port is None or 0 < parts.port <= 65535))
    except ValueError:
        return False


def parse_time(text: str) -> float:
    """Parse nonnegative seconds, MM:SS or HH:MM:SS (fractional seconds OK)."""
    if not isinstance(text, str):
        raise ValueError("Time must be a string")
    parts = text.strip().split(":")
    if not 1 <= len(parts) <= 3 or not re.fullmatch(r"\d+(?:\.\d+)?", parts[-1]):
        raise ValueError("Use seconds, MM:SS or HH:MM:SS")
    if any(not re.fullmatch(r"\d+", part) for part in parts[:-1]):
        raise ValueError("Hours and minutes must be integers")
    values = [float(part) for part in parts]
    if any(not math.isfinite(value) for value in values) or (len(parts) > 1 and values[-1] >= 60) or (len(parts) == 3 and values[1] >= 60):
        raise ValueError("Invalid time component")
    result = sum(value * 60 ** index for index, value in enumerate(reversed(values)))
    if not math.isfinite(result):
        raise ValueError("Time is too large")
    return result


def local_to_utc(text: str, timezone: str) -> datetime:
    """Convert ISO local time to UTC, rejecting DST gaps and unresolved folds."""
    try:
        zone = ZoneInfo(timezone)
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Invalid ISO date/time or IANA timezone") from exc
    wall = parsed.replace(tzinfo=None)
    candidates = set()
    for fold in (0, 1):
        local = wall.replace(tzinfo=zone, fold=fold)
        utc = local.astimezone(dt_timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == wall:
            if parsed.tzinfo is None or parsed.utcoffset() == local.utcoffset():
                candidates.add(utc)
    if not candidates:
        raise ValueError("Nonexistent local time or offset inconsistent with timezone")
    if len(candidates) != 1:
        raise ValueError("Ambiguous local time; supply an explicit ISO offset")
    return candidates.pop()


def _uuid(value):
    try:
        return isinstance(value, str) and bool(UUID(value))
    except (ValueError, AttributeError):
        return False


def _number(value, positive=False):
    try:
        return (type(value) in (int, float) and math.isfinite(value)
                and (value > 0 if positive else value >= 0))
    except OverflowError:
        return False


def _fields(record, limits, required, urls):
    errors = []
    for field, limit in limits.items():
        value = record.get(field, "")
        if not isinstance(value, str):
            errors.append(f"{field} must be text")
        elif len(value) > limit:
            errors.append(f"{field} must be at most {limit} characters")
        elif field in required and not value.strip():
            errors.append(f"{field} is required")
    for field in urls:
        if record.get(field) and not validate_https(record[field]):
            errors.append(f"{field} must be an absolute HTTPS URL")
    for field in ("explicit", "locked", "op3"):
        if field in record and type(record[field]) is not bool:
            errors.append(f"{field} must be a boolean")
    return errors


def validate_show(show) -> list[str]:
    if not isinstance(show, dict):
        return ["show must be a dictionary"]
    urls = ("website", "artwork_url", "funding_url", "base_url")
    limits = dict(title=255, description=4000, author=255, owner_name=255,
                  owner_email=254, copyright=255, language=35, funding_label=128,
                  output_dir=4096, category=100, subcategory=100, secondary_category=100)
    limits.update(dict.fromkeys(urls, 2048))
    errors = _fields(show, limits, ("title", "description", "base_url", "output_dir"), urls)
    from .storage import validate_storage
    errors.extend(validate_storage(show))
    errors.extend(validate_presets(show))
    if validate_https(show.get("base_url")):
        base = urlsplit(show["base_url"])
        if base.query or base.fragment:
            errors.append("base_url must not contain a query string or fragment")
    for field in ("id", "guid"):
        if not _uuid(show.get(field)):
            errors.append(f"{field} must be a UUID")
    if show.get("podcast_type") not in ("episodic", "serial"):
        errors.append("podcast_type must be episodic or serial")
    try:
        ZoneInfo(show.get("timezone"))
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        errors.append("timezone must be an IANA timezone")
    if not isinstance(show.get("language"), str) or not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", show["language"]):
        errors.append("language must be a language tag such as en or en-US")
    email = show.get("owner_email")
    if email and (not isinstance(email, str) or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)):
        errors.append("owner_email must be a valid email address")
    category = show.get("category")
    if category and (not isinstance(category, str) or category not in CATEGORIES):
        errors.append("category must be an Apple primary category")
    if show.get("subcategory") and show["subcategory"] not in CATEGORIES.get(category if isinstance(category, str) else "", []):
        errors.append("subcategory must belong to the primary category")
    secondary = show.get("secondary_category")
    if secondary and (not isinstance(secondary, str) or secondary not in CATEGORIES):
        errors.append("secondary_category must be an Apple primary category")
    if show.get("funding_label") and not show.get("funding_url"):
        errors.append("funding_url is required with funding_label")
    entries = show.get("podroll", [])
    if not isinstance(entries, list):
        errors.append("podroll must be a list")
    else:
        if len(entries) > 8:
            errors.append("podroll must have at most 8 entries")
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                errors.append(f"podroll[{index}] must be a dictionary")
                continue
            if not _uuid(entry.get("feedGuid")):
                errors.append(f"podroll[{index}].feedGuid must be a UUID")
            errors.extend(f"podroll[{index}].{error}" for error in _fields(
                entry, {"feedUrl": 2048, "title": 255}, (), ("feedUrl",)))
    return errors


def validate_presets(show) -> list[str]:
    errors = []
    if show.get("audio_preset", "standard") not in ("standard", "music"):
        errors.append("audio_preset must be standard or music")
    if show.get("image_preset", "compact") not in ("compact", "detail"):
        errors.append("image_preset must be compact or detail")
    return errors


def validate_episode(episode) -> list[str]:
    if not isinstance(episode, dict):
        return ["episode must be a dictionary"]
    urls = ("link", "mp3_url", "artwork_url", "transcript_url")
    errors = _fields(episode, {"title": 60, "description": 4000, **dict.fromkeys(urls, 2048)},
                     ("title", "description", "mp3_url"), urls)
    if not isinstance(episode.get("guid"), str) or not episode["guid"].strip() or any(ord(c) < 32 for c in episode["guid"]):
        errors.append("guid must be nonempty text without control characters")
    slug = episode.get("slug")
    if slug:
        from .models import slug_error
        reason = slug_error(slug)
        if reason:
            errors.append(f"slug {reason}")
    if episode.get("episode_type") not in ("full", "trailer", "bonus"):
        errors.append("episode_type must be full, trailer or bonus")
    for field in ("length", "episode_number", "season_number"):
        value = episode.get(field)
        if value is None and field != "length":
            continue
        if type(value) is not int or value <= 0:
            errors.append(f"{field} must be a positive integer")
    media_duration = episode.get("duration")
    if not _number(media_duration, positive=True):
        errors.append("duration must be a positive finite number of seconds")
    keywords = episode.get("keywords", [])
    if not isinstance(keywords, list) or len(keywords) > 10 or any(not isinstance(k, str) or not k.strip() or len(k) > 255 for k in keywords):
        errors.append("keywords must be a list of at most 10 nonempty strings (255 characters each)")
    for field in ("soundbites", "chapters"):
        entries = episode.get(field, [])
        if not isinstance(entries, list):
            errors.append(f"{field} must be a list")
            continue
        previous_start = previous_end = -1
        for index, entry in enumerate(entries):
            prefix = f"{field}[{index}]"
            if not isinstance(entry, dict):
                errors.append(f"{prefix} must be a dictionary")
                continue
            title = entry.get("title", "")
            limit = 128 if field == "soundbites" else 255
            if not isinstance(title, str) or len(title) > limit:
                errors.append(f"{prefix}.title must be text of at most {limit} characters")
            start = entry.get("startTime")
            if not _number(start):
                errors.append(f"{prefix}.startTime must be nonnegative finite seconds")
                continue
            if field == "soundbites":
                duration = entry.get("duration")
                if not _number(duration, positive=True):
                    errors.append(f"{prefix}.duration must be positive finite seconds")
                    continue
                end = start + duration
                if "stop" in entry and (not _number(entry["stop"]) or not math.isclose(entry["stop"], end)):
                    errors.append(f"{prefix}.stop must equal startTime + duration")
            else:
                end = entry.get("endTime")
                for key in ("img", "url"):
                    if entry.get(key) and not validate_https(entry[key]):
                        errors.append(f"{prefix}.{key} must be an absolute HTTPS URL")
            if not _number(end) or end <= start:
                errors.append(f"{prefix} end must be finite and greater than start")
                continue
            if start < previous_start or (field == "chapters" and start < previous_end):
                errors.append(f"{prefix} must be sorted" + (" and nonoverlapping" if field == "chapters" else ""))
            if _number(media_duration, positive=True) and end > media_duration:
                errors.append(f"{prefix} must end within media duration")
            previous_start, previous_end = start, end
    return errors


class _HTTPSRedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not validate_https(newurl):
            raise ValueError("Redirect must use HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url, method="GET"):
    if not validate_https(url):
        raise ValueError("URL must use HTTPS")
    response = request.build_opener(_HTTPSRedirectHandler()).open(
        request.Request(url, method=method, headers={"User-Agent": "Termicast", "Accept-Encoding": "identity"}),
        timeout=NETWORK_TIMEOUT,
    )
    if not validate_https(response.geturl()):
        response.close()
        raise ValueError("Response URL must use HTTPS")
    return response


def _download(url, target, limit, progress=None):
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    with _open(url) as response:
        total = 0
        expected = int(response.headers.get("Content-Length", "0"))
        if expected > limit:
            raise ValueError("Download exceeds inspection size limit")
        disk = Path(target.name).parent if isinstance(getattr(target, "name", None), str) else None
        if disk and shutil.disk_usage(disk).free < (expected or min(limit, 64 * 1024**2)) + 16 * 1024**2:
            raise OSError("Insufficient disk space for download")
        from rich.progress import Progress, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn
        with Progress("{task.description}", DownloadColumn(), TransferSpeedColumn(), TimeRemainingColumn(), disable=progress is not None) as display:
            task = display.add_task(url, total=expected or None)
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Download exceeded time limit")
                chunk = response.read1(min(65536, limit + 1 - total))
                if not chunk:
                    if expected and total != expected:
                        raise ValueError("Incomplete download: Content-Length does not match received bytes")
                    return
                total += len(chunk)
                if total > limit:
                    raise ValueError("Download exceeds inspection size limit")
                if disk and shutil.disk_usage(disk).free < len(chunk) + 16 * 1024**2:
                    raise OSError("Insufficient disk space for download")
                target.write(chunk)
                display.update(task, completed=total)
                if progress:
                    progress(url, total, expected or None)


def probe_local_media(path):
    result = {"length": Path(path).stat().st_size}
    try:
        process = subprocess.run(
            ["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries",
             "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=NETWORK_TIMEOUT, check=True, stdin=subprocess.DEVNULL)
        duration = float(json.loads(process.stdout)["format"]["duration"])
        if _number(duration, positive=True):
            result["duration"] = duration
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        pass
    return result


def inspect_local_artwork(path, episode=False, chapter=False):
    from PIL import Image
    with Image.open(path) as image:
        size, mode, format_name = image.size, image.mode, image.format
        image.verify()
    if format_name not in ("JPEG", "PNG") or mode != "RGB":
        raise ValueError("Artwork must be JPEG or PNG in RGB mode")
    if episode and size != (3000, 3000):
        raise ValueError("Episode artwork must be exactly 3000x3000 pixels")
    if not episode and not chapter and (size[0] != size[1] or not 1400 <= size[0] <= 3000):
        raise ValueError("Show artwork must be square, 1400 through 3000 pixels (3000 ideal)")
    return []


def probe_media(url) -> dict:
    """Best-effort positive byte length/duration; missing values need manual input."""
    if not validate_https(url):
        raise ValueError("Media URL must use HTTPS")
    result = {}
    try:
        with _open(url, "HEAD") as response:
            length = int(response.headers.get("Content-Length", "0"))
            if length > 0:
                result["length"] = length
    except (OSError, ValueError):
        pass
    if result.get("length", 0) > MAX_MEDIA_BYTES:
        return result
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3") as media:
            _download(url, media, MAX_MEDIA_BYTES)
            media.flush()
            if media.tell() > 0:
                result.setdefault("length", media.tell())
            process = subprocess.run(
                ["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries",
                 "format=duration", "-of", "json", media.name],
                capture_output=True, text=True, timeout=NETWORK_TIMEOUT, check=True,
                stdin=subprocess.DEVNULL,
            )
            duration = float(json.loads(process.stdout)["format"]["duration"])
            if _number(duration, positive=True):
                result["duration"] = duration
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        pass
    return result


def inspect_artwork(url, episode=False) -> list[str]:
    """Inspect via Pillow; return warnings if unavailable, raise on bad dimensions."""
    if not validate_https(url):
        raise ValueError("Artwork URL must use HTTPS")
    try:
        from PIL import Image
    except ImportError:
        return ["Artwork inspection unavailable: Pillow is not installed. Verify dimensions manually."]
    try:
        content = BytesIO()
        _download(url, content, MAX_ARTWORK_BYTES)
        content.seek(0)
        with Image.open(content) as image:
            width, height = image.size
            mode, format_name = image.mode, image.format
            image.verify()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        return [f"Artwork inspection unavailable: {exc}. Verify dimensions manually."]
    if format_name not in ("JPEG", "PNG") or mode != "RGB":
        raise ValueError("Artwork must be JPEG or PNG in RGB mode")
    if episode and (width, height) != (3000, 3000):
        raise ValueError("Episode artwork must be exactly 3000x3000 pixels")
    if not episode and (width != height or not 1400 <= width <= 3000):
        raise ValueError("Show artwork must be square, 1400 through 3000 pixels")
    return []
