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
    return validation.validate_chapter_payload(json.loads(read_asset(source)), episode)
