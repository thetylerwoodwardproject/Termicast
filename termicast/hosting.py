"""Read-only hosting checks (doctor) and shared HTTPS verification helpers.

`doctor` verifies tools, public feed/media accessibility, MIME types, and
local-versus-remote feed content. It is read-only by default and never probes
bucket writes or deletes. Public verification is shared with S3 deployment.
"""

import shutil
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from .storage import asset_base, asset_root


def _head_or_range(url, expected_content_type=None):
    """Return (status, content_type, body) using HEAD, falling back to a ranged GET."""
    from . import validation
    try:
        response = validation._open(url, "HEAD")
        content_type = response.headers.get("Content-Type", "")
        status = getattr(response, "status", response.getcode())
        body = b""
        response.close()
        return status, content_type, body
    except HTTPError as exc:
        # Some servers reject HEAD; fall back to a 1-byte ranged GET.
        try:
            response = validation._open(url)
            content_type = response.headers.get("Content-Type", "")
            body = response.read(1)
            status = response.getcode()
            response.close()
            return status, content_type, body
        except HTTPError as ranged:
            return ranged.code, "", b""
    except (URLError, OSError):
        return None, "", b""


def check_url(url, expected_content_type=None) -> list[str]:
    """Return a list of problems for a public URL; empty list means OK."""
    problems = []
    if not url:
        return problems
    status, content_type, _ = _head_or_range(url)
    if status is None:
        return [f"Unreachable: {url}"]
    if status != 200:
        problems.append(f"HTTP {status}: {url}")
    if expected_content_type:
        base = expected_content_type.split(";")[0].strip()
        if content_type and not content_type.lower().startswith(base.lower()):
            problems.append(f"Unexpected Content-Type {content_type!r} (expected {base}): {url}")
        elif not content_type:
            problems.append(f"Missing Content-Type (expected {base}): {url}")
    return problems


def check_tools(show) -> list[str]:
    problems = []
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            problems.append(f"Missing {tool}: install FFmpeg for audio preparation")
    if show.get("hosting") == "s3" and shutil.which("s4cmd") is None:
        problems.append("Missing s4cmd: install it for S3 deployment")
    return problems


def check_feed(show) -> list[str]:
    """Check the public feed's accessibility, type, and content freshness."""
    problems = []
    feed_url = asset_base(show) + "/feed.xml"
    problems.extend(check_url(feed_url, "application/rss+xml"))
    local = asset_root(show) / "feed.xml"
    if not local.is_file():
        problems.append("Local feed.xml does not exist; generate it first")
        return problems
    local_bytes = local.read_bytes()
    status, _, body = _head_or_range(feed_url)
    if status == 200:
        remote = _full_body(feed_url)
        if remote is None:
            problems.append("Feed is reachable but its full content could not be compared")
        elif remote != local_bytes:
            problems.append("Public feed differs from the local feed; deploy is not current")
    return problems


def _full_body(url):
    from . import validation
    try:
        response = validation._open(url)
        data = response.read()
        response.close()
        return data
    except (HTTPError, URLError, OSError):
        return None


def check_assets(show, episodes) -> list[str]:
    """Check accessibility and MIME types of managed published assets."""
    from .media import content_type_for
    problems = []
    seen = set()
    base = asset_base(show)
    for episode in episodes:
        if episode.get("status") != "published":
            continue
        urls = []
        if episode.get("mp3_url"):
            urls.append((episode["mp3_url"], content_type_for(episode["mp3_url"])))
        if episode.get("artwork_url"):
            urls.append((episode["artwork_url"], "image/jpeg"))
        if episode.get("transcript_url"):
            urls.append((episode["transcript_url"], "text/vtt"))
        if episode.get("chapters"):
            from .models import chapters_relative
            urls.append((base + "/" + chapters_relative(episode), "application/json+chapters"))
        for url, expected in urls:
            if url in seen:
                continue
            seen.add(url)
            problems.extend(check_url(url, expected))
    return problems


def doctor(db, show_id=None) -> list[str]:
    """Return a flat list of read-only problems across selected shows."""
    problems = []
    shows = [db.get_show(show_id)] if show_id else db.list_shows()
    if show_id and shows[0] is None:
        return [f"Unknown podcast ID: {show_id}"]
    for show in shows:
        if show is None:
            continue
        label = f"{show.get('title', show['id'])} ({show['id']})"
        problems.extend(f"{label}: {p}" for p in check_tools(show))
        problems.extend(f"{label}: {p}" for p in check_feed(show))
        episodes = db.list_episodes(show["id"])
        problems.extend(f"{label}: {p}" for p in check_assets(show, episodes))
    return problems


def nginx_snippet(show) -> str:
    """Return a small, placement-specific nginx MIME-type snippet for base_url."""
    path = urlsplit(show["base_url"]).path.rstrip("/") or "/"
    return (
        f"# Termicast hosting snippet for {show['base_url']}\n"
        f"# Place inside the server/location block that maps {path} to the output directory.\n"
        "# This only sets MIME types; it does not configure TLS, aliases, auth, or redirects.\n"
        "types {\n"
        "    application/rss+xml       xml;\n"
        "    application/json+chapters json;\n"
        "    text/vtt                  vtt;\n"
        "    audio/mp4                 m4a;\n"
        "    audio/mpeg                mp3;\n"
        "    image/jpeg                jpg jpeg;\n"
        "}\n"
    )


def apache_snippet(show) -> str:
    """Return a small, placement-specific Apache MIME-type snippet for base_url."""
    return (
        f"# Termicast hosting snippet for {show['base_url']}\n"
        "# Place inside the <Directory> or <Location> block that maps the output directory.\n"
        "# This only sets MIME types; it does not configure TLS, aliases, auth, or redirects.\n"
        "AddType application/rss+xml       .xml\n"
        "AddType application/json+chapters .json\n"
        "AddType text/vtt                  .vtt\n"
        "AddType audio/mp4                 .m4a\n"
        "AddType audio/mpeg                .mp3\n"
        "AddType image/jpeg                .jpg .jpeg\n"
    )
