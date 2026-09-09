"""Bounded optional asset readers shared by import review and episode editing."""

import io
import json
from pathlib import Path

from . import validation


def read_asset(source):
    if source.startswith("https://"):
        handle = io.BytesIO()
        validation._download(source, handle, validation.MAX_ARTWORK_BYTES)
        data = handle.getvalue()
    else:
        with Path(source).expanduser().open("rb") as handle:
            data = handle.read(validation.MAX_ARTWORK_BYTES + 1)
    if len(data) > validation.MAX_ARTWORK_BYTES:
        raise ValueError("Optional asset exceeds size limit")
    return data.decode("utf-8-sig")


def check_vtt(value):
    if not value.startswith("WEBVTT") or (len(value) > 6 and not value[6].isspace()):
        raise ValueError("Transcript must be UTF-8 WebVTT with a WEBVTT header")
    if "-->" not in value:
        raise ValueError("Transcript must contain at least one timed cue")
    return value


def read_chapters(source, episode):
    payload = json.loads(read_asset(source))
    if not isinstance(payload, dict):
        raise ValueError("Chapter JSON must be an object")
    chapters = payload.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise ValueError("Chapter JSON requires a nonempty chapters array")
    if any(not isinstance(chapter, dict) or "startTime" not in chapter for chapter in chapters):
        raise ValueError("Each chapter must be an object with startTime")
    for index, chapter in enumerate(chapters):
        chapter.setdefault("endTime", chapters[index + 1]["startTime"]
                           if index + 1 < len(chapters) else episode["duration"])
    errors = validation.validate_episode(dict(episode, chapters=chapters))
    if errors:
        raise ValueError("; ".join(errors))
    return chapters
