"""Bounded, non-resolving RSS imports returning JSON-compatible profiles."""

from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import NAMESPACE_URL, uuid4, uuid5
from datetime import datetime, timezone
from copy import deepcopy
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
import tempfile

from .feed import MAX_FEED_BYTES, _parse_xml, _tag, _put
from .models import new_episode
from . import validation


def _https(url):
    if not validation.validate_https(url):
        raise ValueError("Feed URL must be HTTPS without credentials")
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError
        parts.port
    except ValueError as exc:
        raise ValueError("Feed URL must be HTTPS without credentials") from exc
    return url


class _HTTPSRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _extract_show(root, source):
    channel = root.find("channel")

    def text(name, default=""):
        return channel.findtext(_tag(name), default=default) or default

    def attr(name, key):
        element = channel.find(_tag(name))
        return element.get(key, "") if element is not None else ""

    feed_url = source
    for link in channel.findall(_tag("atom:link")):
        if link.get("rel") == "self" and link.get("href"):
            feed_url = link.get("href")
            break
    categories = channel.findall(_tag("itunes:category"))
    primary = categories[0] if categories else None
    secondary = categories[1] if len(categories) > 1 else None
    subcategory = primary.find(_tag("itunes:category")) if primary is not None else None
    owner = channel.find(_tag("itunes:owner"))
    roll = channel.find(_tag("podcast:podroll"))
    return {
        "id": str(uuid4()),
        "guid": text("podcast:guid") or str(uuid5(NAMESPACE_URL, source or feed_url)),
        "title": text("title"), "description": text("description"),
        "author": text("itunes:author"),
        "owner_name": owner.findtext(_tag("itunes:name"), "") if owner is not None else "",
        "owner_email": owner.findtext(_tag("itunes:email"), "") if owner is not None else "",
        "website": text("link"), "copyright": text("copyright"),
        "artwork_url": attr("itunes:image", "href") or channel.findtext("image/url", ""),
        "locked": text("podcast:locked").strip().lower() in ("yes", "true", "1"),
        "explicit": text("itunes:explicit").strip().lower() in ("yes", "true", "1", "explicit"),
        "language": text("language", "en"),
        "podcast_type": "serial" if text("itunes:type").strip() == "serial" else "episodic",
        "timezone": "UTC",
        "category": primary.get("text", "") if primary is not None else "",
        "subcategory": subcategory.get("text", "") if subcategory is not None else "",
        "secondary_category": secondary.get("text", "") if secondary is not None else "",
        "funding_url": attr("podcast:funding", "url"), "funding_label": text("podcast:funding"),
        "podroll": [{key: item.get(key) for key in ("feedGuid", "feedUrl", "title") if item.get(key)}
                    for item in roll.findall(_tag("podcast:remoteItem"))] if roll is not None else [],
        "output_dir": "",
        "base_url": feed_url.rsplit("/", 1)[0] if feed_url.startswith("https://") else "",
    }


def import_feed(source: str) -> tuple[dict, bytes]:
    """Import a local file or HTTPS URL, retaining the exact original XML bytes.

    Network reads have a 15-second socket timeout and a 10 MiB body limit.
    Imported output_dir/base_url should be reviewed before publication.
    """
    parts = urlsplit(source)
    if parts.scheme:
        _https(source)
        request = Request(source, headers={"User-Agent": "Termicast/1.0", "Accept": "application/rss+xml, application/xml, text/xml"})
        with build_opener(_HTTPSRedirectHandler()).open(request, timeout=15) as response:
            _https(response.geturl())
            size = response.headers.get("Content-Length")
            if size and int(size) > MAX_FEED_BYTES:
                raise ValueError("Feed exceeds the 10 MiB size limit")
            data = response.read(MAX_FEED_BYTES + 1)
        identity_url = source
    else:
        path = Path(source).expanduser()
        with path.open("rb") as handle:
            data = handle.read(MAX_FEED_BYTES + 1)
        identity_url = path.resolve().as_uri()
    root = _parse_xml(data)
    if not parts.scheme:
        for link in root.find("channel").findall(_tag("atom:link")):
            if link.get("rel") == "self" and link.get("href"):
                identity_url = link.get("href")
                break
    return _extract_show(root, identity_url), data


def rewrite_urls(root, mapping):
    # Identity is based on the source enclosure, never on its rehosted URL.
    for item in root.findall("channel/item"):
        if not item.findtext("guid"):
            enclosure = item.find("enclosure")
            identity = enclosure.get("url", "") if enclosure is not None else ""
            guid = item.find("guid")
            if guid is None:
                guid = _put(item, "guid", isPermaLink="false")
            guid.text = str(uuid5(NAMESPACE_URL, identity))
    for element in root.iter():
        if element.tag in ("guid", _tag("podcast:guid")):
            continue
        if element.text in mapping:
            element.text = mapping[element.text]
        for key, value in list(element.attrib.items()):
            if value in mapping:
                element.set(key, mapping[value])


def extract_episode(item):
    def text(name, default=""):
        return item.findtext(_tag(name), default) or default

    def attr(name, key):
        element = item.find(_tag(name))
        return element.get(key, "") if element is not None else ""

    url = attr("enclosure", "url")
    guid = text("guid") or str(uuid5(NAMESPACE_URL, url))
    date = parsedate_to_datetime(text("pubDate"))
    if date.tzinfo is None:
        raise ValueError(f"Imported episode {guid}: publication date needs a timezone")
    episode = new_episode(
        guid=guid, title=text("title"), description=text("description") or text("content:encoded"),
        link=text("link"), mp3_url=url, length=int(attr("enclosure", "length") or 0),
        duration=validation.parse_time(text("itunes:duration", "0")),
        episode_type=text("itunes:episodeType", "full"),
        explicit=text("itunes:explicit").lower() in ("true", "yes", "1", "explicit"),
        artwork_url=attr("itunes:image", "href") or item.findtext("image/url", ""),
        transcript_url=attr("podcast:transcript", "url"),
        keywords=[s.strip() for s in text("itunes:keywords").split(",") if s.strip()],
        published_at=date.astimezone(timezone.utc).isoformat(),
    )
    for field, tag in (("episode_number", "itunes:episode"), ("season_number", "itunes:season")):
        episode[field] = int(text(tag)) if text(tag) else None
    episode["soundbites"] = [dict(startTime=float(e.get("startTime")), duration=float(e.get("duration")),
                                  title=e.text or "") for e in item.findall(_tag("podcast:soundbite"))]
    for chapter in item.findall(f"{_tag('psc:chapters')}/{_tag('psc:chapter')}"):
        entry = dict(startTime=validation.parse_time(chapter.get("start", "0")), title=chapter.get("title", ""))
        for key, source in (("img", "image"), ("url", "href")):
            if chapter.get(source):
                entry[key] = chapter.get(source)
        episode["chapters"].append(entry)
    for index, chapter in enumerate(episode["chapters"]):
        chapter["endTime"] = (episode["chapters"][index + 1]["startTime"]
                              if index + 1 < len(episode["chapters"]) else episode["duration"])
    return episode


def extract_episodes(template, mapping=None):
    if not template:
        return []
    root = _parse_xml(template)
    rewrite_urls(root, mapping or {})
    episodes = [extract_episode(item) for item in root.findall("channel/item")]
    guids = [e["guid"] for e in episodes]
    if len(set(guids)) != len(guids):
        raise ValueError("Imported feed has ambiguous duplicate episode GUIDs")
    return episodes


def download_import(show, template, review_titles=None, review_optional=None, resolve_optional=None):
    """Stage every supported asset before installing a new public directory.

    The original XML stays untouched. Exact URL substitutions are persisted in
    show settings so unknown XML can survive rehosting and subsequent edits.
    """
    errors = validation.validate_show(show)
    if errors:
        raise ValueError("; ".join(errors))
    output = Path(show["output_dir"]).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Import requires a new, nonexistent output directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    root = _parse_xml(template)
    mapping, local_paths, metadata = {}, {}, {}
    episodes = extract_episodes(template)
    changes = [(e["guid"], e["title"], e["title"][:60].rstrip())
               for e in episodes if len(e["title"]) > 60]
    if changes:
        if review_titles is None or not review_titles(changes):
            raise ValueError("Overlong titles require original/replacement review and confirmation")
        for episode in episodes:
            if len(episode["title"]) > 60:
                episode["title"] = episode["title"][:60].rstrip()
    with tempfile.TemporaryDirectory(prefix=".termicast-import-", dir=output.parent) as temporary:
        stage = Path(temporary) / "public"
        for folder in ("audio", "chapters", "images/episodes", "images/show", "images/chapters", "transcripts"):
            (stage / folder).mkdir(parents=True)

        def asset(url, folder, kind=""):
            if not url:
                return ""
            if url in mapping:
                path = local_paths[url]
            else:
                suffix = Path(urlsplit(url).path).suffix.lower()
                if suffix not in (".mp3", ".jpg", ".jpeg", ".png", ".json", ".vtt", ".srt", ".txt", ".html", ".pdf"):
                    suffix = {"audio": ".mp3", "chapters": ".json", "transcripts": ".vtt"}.get(folder, ".img")
                relative = folder + "/" + hashlib.sha256(url.encode()).hexdigest() + suffix
                path = stage / relative
                try:
                    with path.open("xb") as handle:
                        validation._download(url, handle, validation.MAX_MEDIA_BYTES if folder == "audio" else validation.MAX_ARTWORK_BYTES)
                        handle.flush()
                        os.fsync(handle.fileno())
                except Exception:
                    path.unlink(missing_ok=True)
                    raise
                mapping[url] = show["base_url"].rstrip("/") + "/" + relative
                local_paths[url] = path
            if kind:
                validation.inspect_local_artwork(path, episode=kind == "episode", chapter=kind == "chapter")
            return mapping[url]

        def optional(url, folder, episode):
            while True:
                try:
                    if not url:
                        raise ValueError("Linked resource has no URL")
                    result = asset(url, folder)
                    if folder == "chapters":
                        payload = json.loads(local_paths[url].read_text())
                        chapters = payload["chapters"]
                        if not isinstance(chapters, list) or not chapters:
                            raise ValueError("Chapter JSON requires a nonempty chapters array")
                        if any(not isinstance(chapter, dict) or "startTime" not in chapter for chapter in chapters):
                            raise ValueError("Each chapter must be an object with startTime")
                        for index, chapter in enumerate(chapters):
                            chapter.setdefault("endTime", chapters[index + 1]["startTime"] if index + 1 < len(chapters) else episode["duration"])
                        errors = validation.validate_episode(dict(episode, chapters=chapters))
                        if errors:
                            raise ValueError("; ".join(errors))
                        for chapter in chapters:
                            if chapter.get("img") and chapter["img"] not in mapping.values():
                                chapter["img"] = asset(chapter["img"], "images/chapters", "chapter")
                        from .publisher import atomic_write
                        atomic_write(local_paths[url], (json.dumps(payload, allow_nan=False, indent=2) + "\n").encode())
                        return chapters
                    if Path(urlsplit(url).path).suffix.lower() == ".vtt":
                        from .assets import check_vtt
                        check_vtt(local_paths[url].read_text(encoding="utf-8-sig"))
                    elif not local_paths[url].stat().st_size:
                        raise ValueError("Linked transcript is empty")
                    return result
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    if resolve_optional is None:
                        raise
                    if url in local_paths:
                        local_paths.pop(url).unlink(missing_ok=True)
                        mapping.pop(url, None)
                    url = resolve_optional(episode, folder, url, exc)
                    if url is None:
                        return [] if folder == "chapters" else ""

        channel = root.find("channel")
        for element in channel.findall(_tag("itunes:image")):
            asset(element.get("href"), "images/show", "show")
        asset(channel.findtext("image/url"), "images/show", "show")
        asset(show.get("artwork_url"), "images/show", "show")
        for item, episode in zip(channel.findall("item"), episodes):
            url = episode["mp3_url"]
            episode["mp3_url"] = asset(url, "audio")
            if url not in metadata:
                metadata[url] = validation.probe_local_media(local_paths[url])
            episode.update(metadata[url])
            episode["artwork_url"] = asset(episode["artwork_url"], "images/episodes", "episode")
            asset(item.findtext("image/url"), "images/episodes", "episode")
            for element in item.findall(_tag("podcast:transcript")):
                episode["transcript_url"] = optional(element.get("url"), "transcripts", episode)
                if episode["transcript_url"] != mapping.get(element.get("url")):
                    episode.setdefault("_replace_fields", []).append("transcript_url")
            for element in item.findall(_tag("podcast:chapters")):
                url = element.get("url")
                chapters = optional(url, "chapters", episode)
                for index, chapter in enumerate(chapters):
                    if chapter.get("img") and chapter["img"] not in mapping.values():
                        chapter["img"] = asset(chapter["img"], "images/chapters", "chapter")
                    chapter.setdefault("endTime", chapters[index + 1]["startTime"] if index + 1 < len(chapters) else episode["duration"])
                episode["chapters"] = chapters
                episode.setdefault("_replace_fields", []).append("chapters")
            for chapter in episode["chapters"]:
                if chapter.get("img") and chapter["img"] not in mapping.values():
                    chapter["img"] = asset(chapter["img"], "images/chapters", "chapter")
            errors = validation.validate_episode(episode)
            if errors:
                raise ValueError(f"Imported episode {episode['guid']}: " + "; ".join(errors))
        if review_optional:
            review_optional(episodes, root)
        for episode in episodes:
            for chapter in episode["chapters"]:
                if chapter.get("img") and chapter["img"] not in mapping.values():
                    chapter["img"] = asset(chapter["img"], "images/chapters", "chapter")
            errors = validation.validate_episode(episode)
            if errors:
                raise ValueError("; ".join(errors))
        show = dict(show, output_dir=str(output), import_url_map=mapping)
        show["artwork_url"] = mapping.get(show["artwork_url"], show["artwork_url"])
        from .feed import render_feed
        render_feed(show, template, episodes, datetime.now(timezone.utc))
        os.rename(stage, output)
        directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return show, episodes
