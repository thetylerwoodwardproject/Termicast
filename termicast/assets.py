"""Bounded optional asset readers shared by import review and episode editing."""

import io
import json
from pathlib import Path
import re

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


# Cue timings as SubRip and headerless WebVTT write them: an optional hours
# field, a comma or dot before the fraction, and loose digit counts.
_STAMP = r"(?:(\d{1,4}):)?(\d{1,2}):(\d{1,2})[.,](\d{1,3})"
_TIMING = re.compile(rf"^[ \t]*{_STAMP}[ \t]*-->[ \t]*{_STAMP}(.*)$")
_CUE_SETTINGS = ("vertical", "line", "position", "size", "align", "region")


def _stamp(hours, minutes, seconds, fraction):
    return (f"{int(hours or 0):02d}:{int(minutes):02d}:{int(seconds):02d}"
            f".{fraction.ljust(3, '0')}")


def _settings(rest):
    """Keep WebVTT cue settings; drop SubRip's X1/Y1 coordinates and stray text."""
    kept = [token for token in rest.split() if token.split(":", 1)[0] in _CUE_SETTINGS]
    return (" " + " ".join(kept)) if kept else ""


def _to_vtt(value):
    """Rewrite SubRip (or headerless WebVTT) cues as WebVTT."""
    lines, cues = value.split("\n"), 0
    for index, line in enumerate(lines):
        match = _TIMING.match(line)
        if match:
            cues += 1
            lines[index] = (_stamp(*match.group(1, 2, 3, 4)) + " --> "
                            + _stamp(*match.group(5, 6, 7, 8)) + _settings(match.group(9)))
    if not cues:
        raise ValueError("Transcript must be UTF-8 WebVTT or SubRip text with at "
                         "least one timed cue (00:00:00.000 --> 00:00:04.000)")
    return "WEBVTT\n\n" + "\n".join(lines).lstrip("\n")


def _normalize(value):
    return value.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def check_srt(value):
    """Validate SubRip cues and return the text unchanged; SubRip stays SubRip."""
    if not any(_TIMING.match(line) for line in _normalize(value).split("\n")):
        raise ValueError("Transcript must be UTF-8 SubRip text with at least one "
                         "timed cue (00:00:00,000 --> 00:00:04,000)")
    return value


def check_vtt(value):
    """Return WebVTT text, converting SubRip and headerless cues on the way."""
    value = _normalize(value)
    if not value.startswith("WEBVTT") or (len(value) > 6 and not value[6].isspace()):
        return _to_vtt(value)
    if "-->" not in value:
        raise ValueError("Transcript must contain at least one timed cue")
    return value


def read_chapters(source, episode):
    return validation.validate_chapter_payload(json.loads(read_asset(source)), episode)
