"""JSON-compatible records, GUID rules, and slug-based asset naming."""

from uuid import NAMESPACE_URL, uuid4, uuid5
import hashlib
import re

SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")

# Sequential asset naming: "ep" ignores seasons, "sep" includes them.
NAMING_SCHEMES = ("ep", "sep")
# What to do with episodes the feed leaves unnumbered.
NAMING_FALLBACKS = ("position", "keep", "renumber")

# Editorial slugs produce these relative asset paths. The audio/image
# extension follows the actual prepared format (see media.py).
CHAPTER_EXT = ".json"
TRANSCRIPT_EXT = ".vtt"

# Each public asset URL's stored relative-path field. Imported episodes carry
# only the URL fields; the path fields are recovered from the URL on demand.
ASSET_ROLES = (("mp3_url", "audio_path"), ("artwork_url", "image_path"),
               ("transcript_url", "transcript_path"))


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



def numbered_slug(episode, position=None, *, seasons=True, default_season=None, word="ep") -> str:
    """Sequential, human-readable name: s01ep001 with a season, ep001 without.

    `position` is the episode's one-based place in the show, used when the feed
    declares no episode number. `default_season` names a season for episodes
    that declare none, so a show asked for the s01ep001 style stays consistent.
    `word` is the naming scheme's word ("ep" by default; scheme_slug and
    suggest_slug pass "bonus"/"trailer" for those episode types).
    The padding is a minimum, so a show that passes 999 episodes simply grows
    to ep1000 rather than colliding.
    """
    number = episode.get("episode_number")
    if number is None:
        number = position
    if number is None:
        raise ValueError("Numbered slugs need an episode number or a position")
    season = None
    if seasons:
        season = episode.get("season_number")
        if season is None:
            season = default_season
    prefix = f"s{int(season):02d}" if season is not None else ""
    return f"{prefix}{word}{int(number):03d}"


def scheme_slug(episode, scheme, *, position=None, fallback="position") -> str:
    """Name `episode` under a naming scheme; "" means keep its current name.

    Bonus and trailer episodes always get their own word ("bonus001",
    "trailer001") and never a season prefix, regardless of `scheme`; only
    "full" episodes (the default when episode_type is unset) follow the
    ep/sep choice `scheme` names.
    """
    if scheme not in NAMING_SCHEMES:
        raise ValueError(f"Naming scheme must be one of {', '.join(NAMING_SCHEMES)}")
    if episode.get("episode_number") is None and (fallback != "position" or position is None):
        return ""
    episode_type = episode.get("episode_type") or "full"
    if episode_type in ("bonus", "trailer"):
        return numbered_slug(episode, position, seasons=False, word=episode_type)
    return numbered_slug(episode, position, seasons=scheme == "sep",
                         default_season=1 if scheme == "sep" else None)


def positions_by_type(episodes) -> dict:
    """Map {guid: position}, numbering each episode_type's episodes independently.

    Full episodes, bonus episodes, and trailers each get their own 1-based
    counter, oldest published first, breaking ties by the episodes' original
    list order. A missing/None episode_type counts as "full", so a show with
    no bonus/trailer episodes gets the single global ordering plan_slugs used
    before this helper existed.
    """
    groups = {}
    for index, episode in enumerate(episodes):
        episode_type = episode.get("episode_type") or "full"
        groups.setdefault(episode_type, []).append(index)
    positions = {}
    for indexes in groups.values():
        order = sorted(indexes, key=lambda index: (episodes[index].get("published_at") or "", -index))
        for position, index in enumerate(order, 1):
            positions[episodes[index]["guid"]] = position
    return positions


def plan_slugs(episodes, scheme, fallback="position") -> dict:
    """Map {guid: slug} for a whole show, numbering each episode_type oldest first.

    `fallback` decides what happens to episodes the feed leaves unnumbered:
    "position" numbers them by publication order, "keep" leaves their file
    names alone, and "renumber" ignores the feed's episode numbers entirely
    and numbers every episode by publication order. Full, bonus, and trailer
    episodes are numbered independently (see positions_by_type), so a bonus
    episode becomes "bonus001" without disturbing the full episodes' "ep001"
    sequence.

    Episodes the scheme cannot name are absent from the result. Raises when
    two episodes would claim one name, which happens when a feed repeats an
    episode number within the same episode_type (a numbered trailer, or
    seasons that omit itunes:season); the caller offers another scheme.
    """
    if fallback not in NAMING_FALLBACKS:
        raise ValueError(f"Naming fallback must be one of {', '.join(NAMING_FALLBACKS)}")
    if fallback == "renumber":
        episodes = [dict(episode, episode_number=None, season_number=None)
                    for episode in episodes]
        fallback = "position"
    positions = positions_by_type(episodes)
    slugs = {}
    for episode in episodes:
        slug = scheme_slug(episode, scheme, position=positions.get(episode["guid"]), fallback=fallback)
        if slug:
            slugs[episode["guid"]] = slug
    taken = {}
    for guid, slug in slugs.items():
        taken.setdefault(slug, []).append(guid)
    duplicates = {slug: guids for slug, guids in taken.items() if len(guids) > 1}
    if duplicates:
        raise ValueError("Two episodes would be named the same: " + "; ".join(
            f"{slug} ({', '.join(guids)})" for slug, guids in sorted(duplicates.items())))
    for slug in slugs.values():
        reason = slug_error(slug)
        if reason:
            raise ValueError(f"Computed name {slug!r} is unsafe: {reason}")
    return slugs


def suggest_slug(episode) -> str:
    """Suggest an editorial slug from season/number, else the episode GUID."""
    if episode.get("episode_number") is None:
        return str(episode.get("guid") or uuid4())
    episode_type = episode.get("episode_type") or "full"
    if episode_type in ("bonus", "trailer"):
        return numbered_slug(episode, seasons=False, word=episode_type)
    return numbered_slug(episode)


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


def chapter_image_relative(slug, index, suffix, *, guid=None) -> str:
    """Relative path for a chapter image following the episode's slug.

    `index` is the chapter's one-based position in the episode, `suffix` the
    original file suffix (including the dot). Kept under `images/chapters/` so
    it stays inside the managed folders while sharing the episode's slug.
    Without a slug, supply `guid` to use the managed chapter JSON's safe stem.
    """
    stem = slug or chapter_filename(guid).removesuffix(CHAPTER_EXT)
    return f"images/chapters/{stem}-{int(index):02d}{suffix}"


def new_show(**kwargs) -> dict:
    """Create a show; later base_url changes must not regenerate its GUID."""
    show = dict.fromkeys((
        "title", "description", "author", "owner_name", "owner_email", "website",
        "copyright", "artwork_url", "category", "subcategory", "secondary_category",
        "funding_url", "funding_label", "output_dir", "base_url", "asset_base_url",
    ), "")
    show.update(id="", locked=False, explicit=False, language="en",
                podcast_type="episodic", timezone="UTC", podroll=[],
                hosting="local", endpoint_url="", bucket="", prefix="",
                enabled=False, keep_local_media=False, mirror_feed=False, op3=False,
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
