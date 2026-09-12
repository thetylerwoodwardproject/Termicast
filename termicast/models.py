"""JSON-compatible records, GUID rules, and slug-based asset naming."""

from uuid import NAMESPACE_URL, uuid4, uuid5
import hashlib
import re

SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")

# Editorial slugs produce these relative asset paths. The audio/image
# extension follows the actual prepared format (see media.py).
CHAPTER_EXT = ".json"
TRANSCRIPT_EXT = ".vtt"


def chapter_filename(guid):
    """Keep shipped UUID filenames; opaque imported IDs never become paths."""
    if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", guid):
        return guid + ".json"
    return hashlib.sha256(guid.encode()).hexdigest() + ".json"


def slug_error(slug):
    """Return a human-readable reason the slug is unsafe, or None when valid."""
    if not isinstance(slug, str) or not slug:
        return "Slug must be nonempty"
    if not SLUG_RE.fullmatch(slug):
        return ("Slug must begin with a letter or number and contain only ASCII "
                "letters, numbers, hyphens, and underscores")
    return None


def validate_slug(slug) -> bool:
    return slug_error(slug) is None


def suggest_slug(episode) -> str:
    """Suggest an editorial slug from season/number, else the episode GUID."""
    number = episode.get("episode_number")
    if number is not None:
        season = episode.get("season_number")
        prefix = f"s{int(season):02d}" if season is not None else ""
        return f"{prefix}ep{int(number):03d}"
    return str(episode.get("guid") or uuid4())


def chapters_relative(episode) -> str:
    """Relative path for the managed chapter JSON file."""
    if episode.get("slug"):
        return f"chapters/{episode['slug']}{CHAPTER_EXT}"
    return "chapters/" + chapter_filename(episode["guid"])


def transcript_relative(episode) -> str:
    """Relative path for the managed WebVTT transcript file."""
    if episode.get("slug"):
        return f"transcripts/{episode['slug']}{TRANSCRIPT_EXT}"
    return "transcripts/" + chapter_filename(episode["guid"]) + TRANSCRIPT_EXT


def new_show(**kwargs) -> dict:
    """Create a show; later base_url changes must not regenerate its GUID."""
    show = dict.fromkeys((
        "title", "description", "author", "owner_name", "owner_email", "website",
        "copyright", "artwork_url", "category", "subcategory", "secondary_category",
        "funding_url", "funding_label", "output_dir", "base_url", "asset_base_url",
    ), "")
    show.update(id=str(uuid4()), locked=False, explicit=False, language="en",
                podcast_type="episodic", timezone="UTC", podroll=[],
                hosting="local", endpoint_url="", bucket="", prefix="",
                enabled=False, keep_local_media=False, op3=False,
                audio_preset="standard", image_preset="compact")
    show.update(kwargs)
    if "guid" not in kwargs:
        show["guid"] = str(uuid5(NAMESPACE_URL, show["base_url"].rstrip("/") + "/feed.xml"))
    return show


def new_episode(**kwargs) -> dict:
    """Create an episode with independent mutable collections."""
    episode = dict.fromkeys(("title", "description", "link", "mp3_url",
                             "artwork_url", "transcript_url"), "")
    episode.update(guid=str(uuid4()), length=None, duration=None, episode_type="full",
                   episode_number=None, season_number=None, explicit=False,
                   keywords=[], soundbites=[], chapters=[], slug="",
                   audio_path="", image_path="", transcript_path="")
    episode.update(kwargs)
    if not episode.get("guid"):
        episode["guid"] = str(uuid4())
    return episode
