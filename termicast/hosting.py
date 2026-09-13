"""Read-only hosting checks (doctor) and shared HTTPS verification helpers.

`doctor` verifies tools, public feed/media accessibility, MIME types, and
local-versus-remote feed content. It is read-only by default and never probes
bucket writes or deletes. Public verification is shared with S3 deployment.
"""

import json
import shutil
from urllib.error import HTTPError, URLError

from .storage import asset_base, asset_root, feed_url


def _head_or_range(url):
    """Return (status, content_type, body) using HEAD, falling back to a ranged GET."""
    from . import validation
    try:
        response = validation._open(url, "HEAD")
        content_type = response.headers.get("Content-Type", "")
        status = getattr(response, "status", response.getcode())
        body = b""
        response.close()
        return status, content_type, body
    except HTTPError:
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


_MIME_MARKERS = ("Unexpected Content-Type ", "Missing Content-Type ")


def is_mime_problem(text) -> bool:
    """True when a problem string or exception describes a Content-Type fault.

    _MIME_MARKERS must stay in step with what check_url() emits above, or the
    offer to correct the host's MIME settings silently stops appearing.
    """
    text = str(text)
    return any(marker in text for marker in _MIME_MARKERS)

_MIME_GUIDANCE = {
    "local": (
        "Content-Type mismatches usually mean the web server is serving the wrong MIME "
        "types. Use Termicast → Open podcast → Hosting → Correct host MIME types for "
        "host detection, a guided configuration edit, syntax validation, and reload. "
        "Or apply the fix from Hosting → Nginx MIME snippet "
        "(or Apache MIME snippet) to the server/location block that serves the failing URL, "
        "then reload the server."
    ),
    "s3": (
        "Uploads already set Content-Type, so these usually mean stale object metadata, a "
        "CDN/proxy header override, or a cached response. Inspect the object metadata and any "
        "CDN/proxy overrides for the failing URL. The Nginx/Apache MIME snippets only apply if "
        "such a server actually serves or proxies that URL; retrying alone won't repair "
        "unchanged object metadata because uploads skip unchanged objects (--sync-check)."
    ),
    "mixed": (
        "If the failing URL is served by your web server, apply the fix from Termicast → Open "
        "podcast → Hosting → Correct host MIME types, or use the Nginx MIME snippet "
        "(or Apache MIME snippet) and reload the server. "
        "If it is an S3/CDN URL, uploads already set Content-Type: inspect object metadata and "
        "any CDN/proxy overrides or cached headers for that URL instead."
    ),
}

_OTHER_ERRORS_FIRST = (
    "Resolve the HTTP/unreachable/upload errors above first: a missing or forbidden URL, or "
    "an HTML error page, must be fixed before its response headers can be treated as a MIME "
    "configuration problem."
)


def summarize_verification_problems(problems, limit=5, *, target="mixed"):
    """Cap MIME diagnostics, retain other failures, and append relevant guidance.

    `target` is 'local', 's3', or 'mixed'; `limit` is a nonnegative integer.
    Returns presentation lines without mutating the input.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError("limit must be a nonnegative integer")
    if target not in ("local", "s3", "mixed"):
        raise ValueError("target must be 'local', 's3', or 'mixed'")
    if not problems:
        return []
    mime = []
    other = []
    for problem in problems:
        if isinstance(problem, str) and any(marker in problem for marker in _MIME_MARKERS):
            mime.append(problem)
        else:
            other.append(problem)
    lines = list(other)
    if not mime:
        return lines
    shown = mime[:limit]
    omitted = len(mime) - len(shown)
    lines.extend(shown)
    if omitted > 0:
        lines.append(f"... {omitted} more Content-Type problems omitted ...")
    if other:
        lines.append(_OTHER_ERRORS_FIRST)
    lines.append(_MIME_GUIDANCE[target])
    return lines


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
    public_feed = feed_url(show)
    problems.extend(check_url(public_feed, "application/rss+xml"))
    local = asset_root(show) / "feed.xml"
    if not local.is_file():
        problems.append("Local feed.xml does not exist; generate it first")
        return problems
    local_bytes = local.read_bytes()
    status, _, body = _head_or_range(public_feed)
    if status == 200:
        remote = _full_body(public_feed)
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


# Independent HEADs, and a 200-episode show is ~600 of them; serially that
# is minutes of latency on every publish.
VERIFY_WORKERS = 12


def check_urls(pairs) -> list[str]:
    """Check many (url, expected_content_type) pairs, in the order given.

    urllib has no keep-alive, so concurrency rather than connection reuse is
    what makes a large catalogue verifiable in reasonable time.
    """
    pairs = list(pairs)
    if not pairs:
        return []
    if len(pairs) == 1:
        return check_url(*pairs[0])
    from concurrent.futures import ThreadPoolExecutor
    problems = []
    with ThreadPoolExecutor(max_workers=min(VERIFY_WORKERS, len(pairs))) as pool:
        for found in pool.map(lambda pair: check_url(*pair), pairs):
            problems.extend(found)
    return problems


def asset_urls(show, episodes, skip=()) -> list[tuple]:
    """Managed published asset URLs and the Content-Type each should carry.

    `skip` drops URLs the caller has already verified, so a deploy does not
    check what it just uploaded twice.
    """
    from .media import content_type_for, transcript_type
    pairs = []
    seen = set(skip)
    base = asset_base(show)
    if show.get("artwork_url") and show["artwork_url"] not in seen:
        seen.add(show["artwork_url"])
        pairs.append((show["artwork_url"], content_type_for(show["artwork_url"]) or "image/jpeg"))
    for episode in episodes:
        if episode.get("status") != "published":
            continue
        urls = []
        if episode.get("mp3_url"):
            urls.append((episode["mp3_url"], content_type_for(episode["mp3_url"])))
        if episode.get("artwork_url"):
            urls.append((episode["artwork_url"], content_type_for(episode["artwork_url"]) or "image/jpeg"))
        if episode.get("transcript_url"):
            urls.append((episode["transcript_url"], transcript_type(episode["transcript_url"])))
        if episode.get("chapters"):
            from .models import chapters_relative
            urls.append((base + "/" + chapters_relative(episode), "application/json+chapters"))
            for chapter in episode["chapters"]:
                img = chapter.get("img")
                if img:
                    urls.append((img, content_type_for(img) or "image/jpeg"))
        for url, expected in urls:
            if url in seen:
                continue
            seen.add(url)
            pairs.append((url, expected))
    return pairs


def check_assets(show, episodes, skip=()) -> list[str]:
    """Check accessibility and MIME types of managed published assets."""
    return check_urls(asset_urls(show, episodes, skip))


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
    from .serverfix import mime_snippet
    return mime_snippet("Nginx")


def apache_snippet(show) -> str:
    """Return a small, placement-specific Apache MIME-type snippet for base_url."""
    from .serverfix import mime_snippet
    return mime_snippet("Apache")


def s3_write_policy_snippet(show) -> str:
    """Return a copy-pasteable AWS IAM policy JSON scoped to exactly what
    Termicast needs for this bucket/prefix: list that prefix, and read/write/
    delete objects under it. Nothing broader.

    Attach it to the IAM user or role whose access key lives in ~/.s3cfg.
    Listing a bucket can succeed with read-only credentials while every
    PutObject is denied, so this is offered whenever an upload comes back
    AccessDenied -- a missing s3:PutObject grant is the most common cause.
    """
    bucket = show["bucket"]
    prefix = show.get("prefix", "")
    object_glob = f"{prefix}/*" if prefix else "*"
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "TermicastListPrefix",
                "Effect": "Allow",
                "Action": "s3:ListBucket",
                "Resource": f"arn:aws:s3:::{bucket}",
                "Condition": {"StringLike": {"s3:prefix": [object_glob]}},
            },
            {
                "Sid": "TermicastReadWritePrefix",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                "Resource": f"arn:aws:s3:::{bucket}/{object_glob}",
            },
        ],
    }
    return json.dumps(policy, indent=2)
