"""JSON-compatible records. GUIDs are assigned once, at construction."""

from uuid import NAMESPACE_URL, uuid4, uuid5
import hashlib
import re


def chapter_filename(guid):
    """Keep shipped UUID filenames; opaque imported IDs never become paths."""
    if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", guid):
        return guid + ".json"
    return hashlib.sha256(guid.encode()).hexdigest() + ".json"


def new_show(**kwargs) -> dict:
    """Create a show; later base_url changes must not regenerate its GUID."""
    show = dict.fromkeys((
        "title", "description", "author", "owner_name", "owner_email", "website",
        "copyright", "artwork_url", "category", "subcategory", "secondary_category",
        "funding_url", "funding_label", "output_dir", "base_url",
    ), "")
    show.update(id=str(uuid4()), locked=False, explicit=False, language="en",
                podcast_type="episodic", timezone="UTC", podroll=[])
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
                   keywords=[], soundbites=[], chapters=[])
    episode.update(kwargs)
    if not episode.get("guid"):
        episode["guid"] = str(uuid4())
    return episode
