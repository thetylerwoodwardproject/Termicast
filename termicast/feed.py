"""RSS generation with preservation of imported podcast metadata.

Managed chapters are published as Podcasting 2.0 JSON only; PSC chapter import
remains supported but PSC start markers are no longer generated.
"""

from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
import math
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from lxml import etree
from .models import chapter_filename, chapters_relative
from .media import ENCLOSURE_TYPES
from .storage import asset_base


NS = {
    "podcast": "https://podcastindex.org/namespace/1.0",
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "psc": "http://podlove.org/simple-chapters",
}
MAX_FEED_BYTES = 10 * 1024 * 1024

# OP3 analytics prefix; enclosure URLs are prefixed at render time only, so the
# stored mp3_url stays the real rehosted URL for downloads and validation.
OP3_PREFIX = "https://op3.dev/e/"


def op3_url(show, mp3_url):
    """Return the enclosure URL, OP3-prefixed when the show enables metrics."""
    if show.get("op3") and mp3_url:
        return OP3_PREFIX + mp3_url
    return mp3_url


def _tag(name):
    if ":" in name:
        prefix, local = name.split(":", 1)
        return f"{{{NS[prefix]}}}{local}"
    return name


def _parse_xml(data):
    if len(data) > MAX_FEED_BYTES:
        raise ValueError("Feed exceeds the 10 MiB size limit")
    try:
        root = etree.fromstring(data, etree.XMLParser(
            resolve_entities=False, load_dtd=False, no_network=True,
            remove_blank_text=False, recover=False,
        ))
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise ValueError(f"Invalid feed XML: {exc}") from exc
    if root.getroottree().docinfo.doctype:
        raise ValueError("DTD declarations are not allowed in feeds")
    if root.tag != "rss" or len(root.findall("channel")) != 1:
        raise ValueError("Expected RSS with exactly one channel")
    return root


def _put(parent, name, text=None, **attrs):
    element = etree.SubElement(parent, _tag(name), **attrs)
    if text is not None:
        element.text = str(text)
    return element


def _replace(parent, name, text=None, **attrs):
    for element in parent.findall(_tag(name)):
        parent.remove(element)
    if text is not None or attrs:
        return _put(parent, name, text, **attrs)
    return None


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Publication and build timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _seconds(value):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("Time values must be finite and nonnegative")
    return format(value, ".12g")


def enclosure_type(mp3_url):
    suffix = Path(urlsplit(mp3_url).path).suffix.lower()
    return ENCLOSURE_TYPES.get(suffix, "audio/mpeg")


def render_feed(show: dict, template: bytes | None, episodes: list[dict],
                built_at: datetime, excluded_guids=()) -> bytes:
    """Render published episodes, preserving template items not managed by GUID.

    Missing/None show settings leave imported fields alone; empty strings/lists
    explicitly clear optional settings. The caller selects published episodes.
    """
    if template is None:
        root = etree.Element("rss", version="2.0", nsmap=NS)
        channel = _put(root, "channel")
        original = {}
    else:
        root = _parse_xml(template)
        from .importer import rewrite_urls
        rewrite_urls(root, show.get("import_url_map", {}))
        namespaces = dict(root.nsmap)
        for prefix, uri in NS.items():
            if uri not in namespaces.values():
                candidate = prefix
                while candidate in namespaces:
                    candidate += "_"
                namespaces[candidate] = uri
        replacement = etree.Element(root.tag, attrib=root.attrib, nsmap=namespaces)
        replacement.text, replacement.tail = root.text, root.tail
        replacement.extend(root)
        root = replacement
        channel = root.find("channel")
        from .importer import _extract_show
        original = _extract_show(root, "")

    def changed(key):
        return key in show and show[key] is not None and (
            template is None or show[key] != original.get(key))

    for key, name in {
        "title": "title", "description": "description", "author": "itunes:author",
        "website": "link", "copyright": "copyright", "language": "language",
        "podcast_type": "itunes:type",
    }.items():
        if changed(key):
            _replace(channel, name, show[key] or None)
    if not (channel.findtext("link") or "").strip() and show.get("base_url"):
        _replace(channel, "link", show["base_url"].rstrip("/"))
    for key, name in {"explicit": "itunes:explicit", "locked": "podcast:locked"}.items():
        if changed(key):
            _replace(channel, name, ("true" if show[key] else "false")
                     if key == "explicit" else ("yes" if show[key] else "no"))
    if channel.find(_tag("podcast:guid")) is None and show.get("guid"):
        _put(channel, "podcast:guid", show["guid"])
    if channel.find(_tag("podcast:medium")) is None:
        _put(channel, "podcast:medium", "podcast")
    _replace(channel, "generator", "Termicast")
    _replace(channel, "lastBuildDate", format_datetime(_utc(built_at), usegmt=True))
    if show.get("base_url"):
        for link in channel.findall(_tag("atom:link")):
            if link.get("rel") == "self":
                channel.remove(link)
        _put(channel, "atom:link", href=show["base_url"].rstrip("/") + "/feed.xml",
             rel="self", type="application/rss+xml")
    for key, name in {"owner_name": "itunes:name", "owner_email": "itunes:email"}.items():
        if changed(key):
            owner = channel.find(_tag("itunes:owner"))
            if owner is None:
                owner = _put(channel, "itunes:owner")
            _replace(owner, name, show[key] or None)
    if changed("artwork_url"):
        _replace(channel, "itunes:image", **({"href": show["artwork_url"]} if show["artwork_url"] else {}))
        if show["artwork_url"]:
            image = channel.find("image")
            if image is None:
                image = _put(channel, "image")
            _replace(image, "url", show["artwork_url"])
            _replace(image, "title", show.get("title") or channel.findtext("title", ""))
            _replace(image, "link", show.get("website") or channel.findtext("link", ""))
        else:
            _replace(channel, "image")
    image = channel.find("image")
    if image is not None:
        for key, name in (("title", "title"), ("website", "link")):
            if changed(key):
                _replace(image, name, show[key] or (channel.findtext("link") if name == "link" else None))
    if any(changed(key) for key in ("category", "subcategory", "secondary_category")):
        settings = original | {key: value for key, value in show.items() if value is not None}
        _replace(channel, "itunes:category")
        if settings.get("category"):
            category = etree.SubElement(channel, _tag("itunes:category"), text=settings["category"])
            if settings.get("subcategory"):
                etree.SubElement(category, _tag("itunes:category"), text=settings["subcategory"])
        if settings.get("secondary_category"):
            etree.SubElement(channel, _tag("itunes:category"), text=settings["secondary_category"])
    if changed("funding_url") or changed("funding_label"):
        url = show.get("funding_url") if show.get("funding_url") is not None else original.get("funding_url")
        label = show.get("funding_label") if show.get("funding_label") is not None else original.get("funding_label")
        _replace(channel, "podcast:funding", (label or None) if url else None,
                 **({"url": url} if url else {}))
    if changed("podroll"):
        _replace(channel, "podcast:podroll")
        if show["podroll"]:
            roll = _put(channel, "podcast:podroll")
            for entry in show["podroll"]:
                _put(roll, "podcast:remoteItem", **{
                    key: str(entry[key]) for key in ("feedGuid", "feedUrl", "title") if entry.get(key)
                })

    managed = {}
    for episode in sorted(episodes, key=lambda item: _utc(item["published_at"]), reverse=True):
        managed.setdefault(str(episode["guid"]), episode)
    originals = {}
    for item in list(channel.findall("item")):
        guid = item.findtext("guid")
        if not guid:
            from .importer import extract_episode
            guid = extract_episode(item)["guid"]
        if guid in excluded_guids:
            channel.remove(item)
            continue
        if guid in managed:
            originals[guid] = item
            channel.remove(item)
    new_items = []
    for guid, episode in managed.items():
        item = etree.Element("item")
        _put(item, "title", episode["title"])
        _put(item, "description", episode["description"])
        _put(item, "content:encoded", episode["description"])
        _put(item, "guid", guid, isPermaLink="false")
        if episode.get("link"):
            _put(item, "link", episode["link"])
        _put(item, "enclosure", url=op3_url(show, episode["mp3_url"]), length=str(int(episode["length"])),
             type=enclosure_type(episode["mp3_url"]))
        _put(item, "pubDate", format_datetime(_utc(episode["published_at"]), usegmt=True))
        _put(item, "itunes:duration", _seconds(episode["duration"]))
        _put(item, "itunes:episodeType", episode.get("episode_type", "full"))
        _put(item, "itunes:explicit", "true" if episode.get("explicit") else "false")
        for key, name in {"episode_number": "itunes:episode", "season_number": "itunes:season"}.items():
            if episode.get(key) is not None:
                _put(item, name, episode[key])
        if episode.get("artwork_url"):
            _put(item, "itunes:image", href=episode["artwork_url"])
        if episode.get("transcript_url"):
            _put(item, "podcast:transcript", url=episode["transcript_url"], type="text/vtt")
        if episode.get("keywords"):
            _put(item, "itunes:keywords", ",".join(episode["keywords"]))
        for soundbite in episode.get("soundbites", []):
            _put(item, "podcast:soundbite", soundbite.get("title", ""),
                 startTime=_seconds(soundbite["startTime"]), duration=_seconds(soundbite["duration"]))
        if episode.get("chapters"):
            _put(item, "podcast:chapters", url=asset_base(show) + "/" + chapters_relative(episode),
                 type="application/json+chapters")
        if guid in originals:
            from .importer import extract_episode
            original_item = originals[guid]
            if original_item.find("guid") is None:
                _put(original_item, "guid", guid, isPermaLink="false")
            baseline = extract_episode(original_item)
            groups = {
                "title": ("title",), "description": ("description", "content:encoded"),
                "link": ("link",), "mp3_url": ("enclosure",), "length": ("enclosure",),
                "published_at": ("pubDate",), "duration": ("itunes:duration",),
                "episode_type": ("itunes:episodeType",), "explicit": ("itunes:explicit",),
                "episode_number": ("itunes:episode",), "season_number": ("itunes:season",),
                "artwork_url": ("itunes:image", "image"), "transcript_url": ("podcast:transcript",),
                "keywords": ("itunes:keywords",), "soundbites": ("podcast:soundbite",),
                "chapters": ("podcast:chapters", "psc:chapters"),
            }
            replaced = {_tag(tag) for key, tags in groups.items() if episode.get(key) != baseline.get(key) or key in episode.get("_replace_fields", [])
                        for tag in tags}
            if show.get("op3") and episode.get("mp3_url"):
                replaced.add(_tag("enclosure"))
            for child in list(original_item):
                if child.tag in replaced:
                    original_item.remove(child)
            for child in list(item):
                if child.tag in replaced:
                    original_item.append(child)
            item = original_item
        new_items.append(item)
    first_item = next((i for i, child in enumerate(channel) if child.tag == "item"), len(channel))
    for index, item in enumerate(new_items):
        channel.insert(first_item + index, item)
    return etree.tostring(root, encoding="UTF-8", xml_declaration=True)


def validate_feed(data: bytes) -> list[str]:
    """Return actionable structural/publication errors, never fetch resources."""
    try:
        root = _parse_xml(data)
    except ValueError as exc:
        return [str(exc)]
    errors = []
    channel = root.find("channel")
    if root.get("version") != "2.0":
        errors.append("RSS version must be 2.0")
    for name in ("title", "description", "link"):
        if not (channel.findtext(name) or "").strip():
            errors.append(f"Channel is missing {name}")
    show_guid = channel.findtext(_tag("podcast:guid"))
    if show_guid is not None:
        try:
            UUID(show_guid.strip())
        except ValueError:
            errors.append("Channel podcast:guid must be a UUID")
    podcast_type = channel.findtext(_tag("itunes:type"))
    if podcast_type is not None and podcast_type not in ("episodic", "serial"):
        errors.append("Channel itunes:type must be episodic or serial")
    seen = set()
    for index, item in enumerate(channel.findall("item"), 1):
        label = f"Item {index}"
        guid = (item.findtext("guid") or "").strip()
        if not guid:
            errors.append(f"{label}: missing guid")
        elif guid in seen:
            errors.append(f"{label}: duplicate guid {guid}")
        seen.add(guid)
        if not (item.findtext("title") or "").strip():
            errors.append(f"{label}: missing title")
        for name, limit in (("title", 60), ("description", 4000)):
            if len(item.findtext(name, "")) > limit:
                errors.append(f"{label}: {name} exceeds {limit} characters")
        episode_type = item.findtext(_tag("itunes:episodeType"))
        if episode_type is not None and episode_type not in ("full", "trailer", "bonus"):
            errors.append(f"{label}: episode type must be full, trailer, or bonus")
        keywords = item.findtext(_tag("itunes:keywords"), "")
        if len([keyword for keyword in keywords.split(",") if keyword.strip()]) > 10:
            errors.append(f"{label}: no more than ten keywords are allowed")
        for name in ("itunes:episode", "itunes:season"):
            value = item.findtext(_tag(name))
            if value is not None:
                try:
                    if int(value) < 1:
                        raise ValueError
                except ValueError:
                    errors.append(f"{label}: {name} must be a positive integer")
        duration = item.findtext(_tag("itunes:duration"))
        if duration is not None:
            try:
                parts = duration.split(":")
                if len(parts) > 3 or any(not part.strip() for part in parts):
                    raise ValueError
                for part in parts:
                    _seconds(part)
                if len(parts) > 1 and any(float(part) >= 60 for part in parts[1:]):
                    raise ValueError
            except (ValueError, OverflowError):
                errors.append(f"{label}: duration must be nonnegative seconds or HH:MM:SS")
        enclosure = item.find("enclosure")
        if enclosure is None:
            errors.append(f"{label}: missing enclosure")
        else:
            if not enclosure.get("url", "").startswith(("https://", "http://")):
                errors.append(f"{label}: enclosure URL must be an HTTP(S) URL")
            try:
                if int(enclosure.get("length", "")) < 0:
                    raise ValueError
            except ValueError:
                errors.append(f"{label}: enclosure length must be a nonnegative integer")
            if not enclosure.get("type"):
                errors.append(f"{label}: enclosure is missing its MIME type")
        try:
            published = parsedate_to_datetime(item.findtext("pubDate", ""))
            if published.tzinfo is None:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            errors.append(f"{label}: pubDate must be an RFC 2822 date with timezone")
    return errors
