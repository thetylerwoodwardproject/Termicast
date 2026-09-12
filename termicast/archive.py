"""Archive/restage ingestion: import a show from a persisted archive.

An archive is a directory containing a `manifest.json` plus staged files:

```json
{
  "version": 1,
  "feed": "feed.xml",
  "pages": ["pages/page-2.xml"],
  "assets": {"https://old.example.org/audio.mp3": "audio/episode.mp3"}
}
```

`feed` and `pages` are feed XML relative to the manifest. `assets` maps each
original URL to its staged relative path under the archive directory; those
paths are installed verbatim (no re-hashing, no re-download). This is the
adapter the guided migration flow uses so a catalog is downloaded once and
can be re-imported without fetching it again.
"""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from lxml import etree

from .feed import _parse_xml, _tag
from .importer import download_import, import_feed


_SUPPORTED_SUFFIXES = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac",
                       ".jpg", ".jpeg", ".png", ".json", ".vtt", ".srt", ".txt", ".html", ".pdf"}


def _default_suffix(folder):
    return {"audio": ".mp3", "chapters": ".json", "transcripts": ".vtt"}.get(folder, ".img")


def _next_link(root):
    channel = root.find("channel")
    for link in channel.findall(_tag("atom:link")):
        if link.get("rel") == "next" and link.get("href"):
            return link.get("href")
    return None


def _collect_urls(root):
    urls = []
    for element in root.iter():
        if element.tag == "enclosure":
            url = element.get("url")
        elif element.tag == _tag("itunes:image"):
            url = element.get("href")
        elif element.tag == "image":
            url = element.get("url")
        elif element.tag == _tag("podcast:transcript"):
            url = element.get("url")
        elif element.tag == _tag("podcast:chapters"):
            url = element.get("url")
        elif element.tag == _tag("psc:chapter"):
            url = element.get("image") or element.get("href")
        else:
            continue
        if url:
            urls.append(url)
    return urls


def _folder_for(url):
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac"}:
        return "audio"
    if suffix in {".jpg", ".jpeg", ".png"}:
        return "images"
    if suffix in {".vtt", ".srt", ".txt"}:
        return "transcripts"
    return "images"


def archive_feed(source, destination, *, max_pages=50):
    """Download a feed, its paginated pages, and referenced assets into a
    persistent archive directory with a `manifest.json`. Returns the manifest
    path. Retries can re-read the archive without downloading the catalog again.
    """
    from . import validation
    destination = Path(destination).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    _, template = import_feed(source)
    root = _parse_xml(template)
    pages = []
    next_url = _next_link(root)
    while next_url and len(pages) < max_pages:
        page_bytes = import_feed(next_url)[1]
        page_root = _parse_xml(page_bytes)
        pages.append((next_url, page_bytes))
        next_url = _next_link(page_root)

    assets = {}

    def download(url, folder):
        if not url:
            return ""
        if url in assets:
            return assets[url]
        suffix = Path(urlsplit(url).path).suffix.lower()
        if suffix not in _SUPPORTED_SUFFIXES:
            suffix = _default_suffix(folder)
        relative = folder + "/" + hashlib.sha256(url.encode()).hexdigest() + suffix
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            with target.open("xb") as handle:
                limit = validation.MAX_MEDIA_BYTES if folder == "audio" else validation.MAX_ARTWORK_BYTES
                validation._download(url, handle, limit)
                handle.flush()
                os.fsync(handle.fileno())
        assets[url] = relative
        return relative

    roots = [root] + [_parse_xml(page_bytes) for _, page_bytes in pages]
    chapter_urls = []
    for page_root in roots:
        for url in _collect_urls(page_root):
            if Path(urlsplit(url).path).suffix.lower() == ".json":
                chapter_urls.append(url)
            else:
                download(url, _folder_for(url))
    # Chapter JSON may reference chapter artwork; download those too.
    for url in chapter_urls:
        relative = download(url, "chapters")
        if not relative:
            continue
        try:
            payload = json.loads((destination / relative).read_text(encoding="utf-8"))
            for chapter in payload.get("chapters", []):
                if chapter.get("img"):
                    download(chapter["img"], "images/chapters")
        except (ValueError, OSError):
            pass

    (destination / "feed.xml").write_bytes(template)
    manifest_pages = []
    for index, (_, page_bytes) in enumerate(pages, 1):
        name = f"pages/page-{index}.xml"
        (destination / name).parent.mkdir(parents=True, exist_ok=True)
        (destination / name).write_bytes(page_bytes)
        manifest_pages.append(name)
    manifest = {"version": 1, "feed": "feed.xml", "pages": manifest_pages, "assets": assets}
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return manifest_path


def restage(manifest_path, base_url):
    """Preview URL restaging: rewrite asset URLs to `base_url` and validate.

    Returns `(mapping, errors)` where `mapping` is `{original_url: new_url}` and
    `errors` lists validation problems in the restaged feed. Nothing is written.
    """
    from .importer import rewrite_urls
    manifest_path, data = load_manifest(manifest_path)
    template = merged_template(manifest_path, data)
    root = _parse_xml(template)
    mapping = {url: base_url.rstrip("/") + "/" + relative
               for url, relative in (data.get("assets") or {}).items()}
    rewrite_urls(root, mapping)
    restaged = etree.tostring(root, encoding="UTF-8", xml_declaration=True)
    from .feed import validate_feed
    errors = validate_feed(restaged)
    return mapping, errors


def _safe_relative_path(value, what):
    """Reject absolute paths and traversal components in manifest-supplied paths.

    `feed`, `pages`, and `assets` values are later joined onto the archive
    directory (to read) and a staging directory (to write); an unvalidated
    absolute path or `..` segment would let a crafted manifest read or write
    files anywhere on disk.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"Archive manifest {what} must be a nonempty relative path")
    if value.startswith("/") or "\\" in value or "\x00" in value:
        raise ValueError(f"Archive manifest {what} must be a relative path: {value!r}")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError(f"Archive manifest {what} must not contain empty, '.', or '..' components: {value!r}")
    return value


def load_manifest(manifest_path):
    path = Path(manifest_path).expanduser()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data.get("feed"):
        raise ValueError("Archive manifest must be a JSON object with a 'feed' entry")
    _safe_relative_path(data["feed"], "feed")
    for page in data.get("pages") or []:
        _safe_relative_path(page, "pages entry")
    for url, relative in (data.get("assets") or {}).items():
        _safe_relative_path(relative, f"assets entry for {url!r}")
    return path, data


def merged_template(manifest_path, data):
    """Return the combined feed bytes, gathering items from all archived pages."""
    root_dir = Path(manifest_path).parent
    template = (root_dir / data["feed"]).read_bytes()
    root = _parse_xml(template)
    if data.get("pages"):
        channel = root.find("channel")
        for page in data["pages"]:
            page_root = _parse_xml((root_dir / page).read_bytes())
            for item in page_root.findall("channel/item"):
                channel.append(deepcopy(item))
        template = etree.tostring(root, encoding="UTF-8", xml_declaration=True)
    return template


def archive_identity(manifest_path):
    """Extract the show identity from an archive's combined feed, without install."""
    manifest_path, data = load_manifest(manifest_path)
    template = merged_template(manifest_path, data)
    root = _parse_xml(template)
    from .importer import _extract_show
    feed = str((Path(manifest_path).parent / data["feed"]))
    return _extract_show(root, feed)


def import_archive(show, manifest_path, review_artwork=None):
    """Consume an archive manifest + staged files into `show`'s asset root.

    Returns `(show, episodes, template)` where `show` carries `import_url_map`
    and `asset_files`. Identity (GUIDs, chapters, extension data) is preserved
    by the shared local-ingestion path; nothing is downloaded again.
    """
    manifest_path, data = load_manifest(manifest_path)
    root_dir = Path(manifest_path).parent
    template = merged_template(manifest_path, data)
    preseed = {url: (str(root_dir / relative), relative)
               for url, relative in (data.get("assets") or {}).items()}
    show, episodes = download_import(show, template, preseed=preseed, require_preseed=True,
                                     review_artwork=review_artwork)
    return show, episodes, template
