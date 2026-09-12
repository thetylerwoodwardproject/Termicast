"""Numbered, dictionary-based interactive forms for Termicast."""

from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import math
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from .models import new_episode, new_show, slug_error, suggest_slug
from .faq import FAQ
from .validation import (
    CATEGORIES, inspect_artwork, local_to_utc, parse_time, probe_media,
    validate_episode, validate_show,
)

ACCENT = "#c0ff00"
console = Console()
_backup_action = ContextVar("termicast_backup_action", default=None)


class ExitRequested(BaseException):
    """Unwind nested menus without an action handler swallowing global exit."""


@contextmanager
def menu_utilities(backup_action):
    token = _backup_action.set(backup_action)
    try:
        yield
    finally:
        _backup_action.reset(token)


def show_faq():
    console.print(Markdown(FAQ))


SHOW_FIELDS = (
    "title", "description", "author", "owner_name", "owner_email", "website",
    "copyright", "artwork_url", "locked", "explicit", "language", "podcast_type",
    "timezone", "category", "subcategory", "secondary_category", "funding_url",
    "funding_label", "podroll", "output_dir", "base_url", "audio_preset",
    "image_preset", "hosting", "op3",
)
EPISODE_FIELDS = (
    "title", "description", "link", "mp3_url", "length", "duration", "episode_type",
    "episode_number", "season_number", "explicit", "artwork_url", "transcript_url",
    "keywords", "soundbites", "chapters",
)
EPISODE_DETAIL_FIELDS = (
    "title", "description", "link", "episode_type", "episode_number", "season_number",
    "explicit", "keywords",
)
SHOW_ESSENTIALS = (
    "title", "description", "author", "owner_email", "artwork_url",
    "explicit", "timezone", "category", "output_dir", "base_url",
)

EXPORT_NOTICE = (
    "Smaller files help listeners on slower connections and reduce hosting bandwidth. "
    "Standard audio uses MP3 at 128 kbps and 44.1 kHz. Compact artwork targets JPEG "
    "below 500 KB. You can change these presets or keep compatible original files."
)


def show_export_notice():
    console.print(EXPORT_NOTICE, style="yellow", markup=False)


def missing_media_metadata(episode):
    for field in ("length", "duration"):
        if not episode.get(field):
            edit_field(episode, field, episode=True)


def error(message):
    console.print(str(message), style="red", markup=False)


def warning(message):
    console.print(str(message), style="yellow", markup=False)


def menu(title, options, default=None):
    """Return a one-based selection. EOF and interrupts propagate to the CLI."""
    while True:
        console.print(title, style=ACCENT, markup=False)
        for index, option in enumerate(options, 1):
            console.print(f"  {index}. {option}", markup=False)
        console.print("  B. Backup saved data    F. FAQ    X. Exit", style=ACCENT)
        answer = console.input("[" + ACCENT + "]Choice[/]" +
                               (f" [{default}]" if default is not None else "") + ": ").strip()
        if answer.lower() == "x":
            raise ExitRequested()
        if answer.lower() in ("b", "f"):
            try:
                if answer.lower() == "f":
                    show_faq()
                else:
                    action = _backup_action.get()
                    if action is None:
                        raise ValueError("Backup is available within a Termicast session")
                    action()
            except EOFError:
                raise
            except Exception as exc:
                error(f"Menu action failed: {exc}")
            continue
        if not answer and default is not None:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)
        error(f"Enter a number from 1 to {len(options)}, B for backup, F for FAQ, or X to exit.")


# Keys are prompt labels without parenthesized requirements, shared by forms
# and CLI actions so examples stay consistent when a field is edited later.
PROMPT_EXAMPLES = {
    "title": "The hidden cost of fast internet",
    "description": "A look at how internet providers price and deliver broadband.",
    "author": "Alex Morgan",
    "owner name": "Alex Morgan",
    "owner email": "alex@example.com",
    "website": "https://example.com/my-show",
    "copyright": "© 2026 Alex Morgan",
    "language": "en (English), en-US (US English), or es (Spanish)",
    "link": "https://example.com/my-show/episodes/episode-42",
    "mp3 url": "https://media.example.com/my-show/audio/episode-42.mp3",
    "artwork url": "https://media.example.com/my-show/cover.png",
    "transcript url": "https://media.example.com/my-show/transcripts/episode-42.vtt",
    "funding url": "https://example.com/my-show/support",
    "funding label": "Support the show",
    "output dir": "/var/www/html/my-show",
    "base url": "https://example.com/my-show",
    "new output directory": "/var/www/html/my-show",
    "existing local source file": "/home/alex/podcast/episode-42.mp3",
    "private backup directory": "/home/alex/backups/termicast",
    "csv path": "/home/alex/podcast/episodes.csv",
    "private csv destination": "/home/alex/backups/episodes.csv",
    "archive manifest path": "/home/alex/podcast-archive/manifest.json",
    "existing feed": "https://old.example.com/feed.xml or /home/alex/podcast/feed.xml",
    "replacement https url": "https://media.example.com/my-show/transcripts/episode-42.vtt",
    "https s3 api endpoint": "https://us-east-1.linodeobjects.com",
    "bucket name": "podcast-media",
    "show prefix": "my-show",
    "public asset url of the bucket/prefix": "https://podcast-media.us-east-1.linodeobjects.com/my-show",
    "iana timezone": "America/New_York, Europe/London, or UTC",
    "length": "28800000 (file size in bytes, not MB)",
    "duration": "00:30:00 or 1800 (a 30-minute episode)",
    "episode number": "42",
    "season number": "2",
    "keywords": "technology, internet, broadband",
    "feed guid": "5eaf7b5e-cc24-5e12-9c73-96c1f81c3b12",
    "feed url": "https://example.com/another-show/feed.xml",
    "start": "00:01:30 or 90 (starts at 90 seconds)",
    "stop": "00:02:00 or 120 (ends at 120 seconds)",
    "chapter artwork url": "https://media.example.com/my-show/images/chapter-1.png",
    "chapter link": "https://example.com/my-show/notes#chapter-1",
    "chapter json path or https url": "/home/alex/podcast/chapters.json or https://example.com/chapters.json",
    "vtt path or https url": "/home/alex/podcast/episode-42.vtt or https://example.com/episode-42.vtt",
    "slug": "s02ep042 or the-hidden-cost-of-internet",
    "audio file path": "/home/alex/podcast/episode-42.mp3",
    "artwork file path": "/home/alex/podcast/cover.png",
    "transcript file path": "/home/alex/podcast/episode-42.vtt",
}

PROMPT_HELP = {
    "output dir": "Local folder where Termicast writes feed.xml and public files. Your web server must serve this folder.",
    "new output directory": "Local destination folder for the podcast's public files.",
    "base url": "Public HTTPS address serving the output directory. Enter the directory URL; Termicast appends /feed.xml.",
    "public asset url of the bucket/prefix": "Public HTTPS directory for uploaded media, including the show prefix. Use the listener-facing URL.",
    "show prefix": "Folder-like path inside the bucket; omit leading and trailing slashes. Blank uses the bucket root.",
}


def text(label, default="", required=False, *, example=None):
    """Blank retains a default; a single '-' explicitly clears an optional field."""
    while True:
        console.print(label, style=ACCENT, markup=False)
        key = label.split(" (", 1)[0].casefold()
        if key in PROMPT_HELP:
            console.print(PROMPT_HELP[key], style="dim", markup=False)
        sample = example if example is not None else PROMPT_EXAMPLES.get(key)
        if sample:
            console.print(f"Example: {sample}", style="dim", markup=False)
        if default is not None and str(default) != "":
            console.print(f"Current: {default}", markup=False)
        value = console.input("Value (Enter keeps current; - clears): ").strip()
        value = "" if value == "-" else value if value else str(default if default is not None else "")
        if value or not required:
            return value
        error("This field is required.")


def confirm(label, default=False):
    return menu(label, ["Yes", "No"], 1 if default else 2) == 1


def choice(label, values, current="", optional=False):
    values = list(values)
    if current and current not in values:
        values.append(current)
    if optional:
        values.insert(0, "")
    default = values.index(current) + 1 if current in values else None
    return values[menu(label, [value or "None" for value in values], default) - 1]


def number(label, current=None, integer=False, optional=False, positive=False):
    while True:
        raw = text(label, current, required=not optional)
        if not raw and optional:
            return None
        try:
            value = int(raw) if integer else parse_time(raw)
            if not math.isfinite(value) or value < 0 or (positive and value == 0):
                raise ValueError("Enter a positive value." if positive else "Enter a nonnegative value.")
            return value
        except (ValueError, OverflowError) as exc:
            error(f"Invalid number or time: {exc}")


def review(title, data):
    table = Table(title=title, border_style=ACCENT)
    table.add_column("Field", style=ACCENT)
    table.add_column("Value", overflow="fold")
    for key, value in data.items():
        table.add_row(key.replace("_", " ").title(), Text(str(value if value is not None else "")))
    console.print(table)


def _artwork(current, episode):
    while True:
        url = text("Artwork URL (HTTPS; episode artwork must be 3000x3000)", current)
        if not url or not confirm("Inspect remote artwork dimensions?", True):
            return url
        try:
            for message in inspect_artwork(url, episode=episode):
                warning(message)
            return url
        except ValueError as exc:
            error(exc)
            current = url


def _podroll(current):
    entries = deepcopy(current or [])
    while True:
        review("Podroll", {str(i + 1): entry for i, entry in enumerate(entries)})
        action = menu("Podroll", ["Add entry", "Edit entry", "Remove entry", "Done"], 4)
        if action == 4:
            return entries
        if action == 1 and len(entries) >= 8:
            error("A podroll can contain at most eight entries.")
            continue
        if action != 1:
            if not entries:
                warning("No entries yet.")
                continue
            index = menu("Entry", [entry.get("title") or entry.get("feedGuid", "Untitled")
                                   for entry in entries] + ["Back"]) - 1
            if index == len(entries):
                continue
            if action == 3:
                entries.pop(index)
                continue
            entry = entries[index]
        else:
            entry = {}
        updated = {key: text(label, entry.get(key, ""), required=key == "feedGuid")
                   for key, label in (("feedGuid", "Feed GUID (UUID, required)"),
                                      ("feedUrl", "Feed URL (optional HTTPS)"),
                                      ("title", "Title (optional)"))}
        if action == 1:
            entries.append(updated)
        else:
            entries[index] = updated


def _segments(current, soundbites):
    entries = deepcopy(current or [])
    name = "Soundbites" if soundbites else "Chapters"
    while True:
        review(name, {str(i + 1): entry for i, entry in enumerate(entries)})
        action = menu(name, ["Add", "Edit", "Remove", "Done"], 4)
        if action == 4:
            return entries
        index = len(entries)
        old = {}
        if action != 1:
            if not entries:
                warning("No entries yet.")
                continue
            index = menu("Select entry", [entry.get("title", "Untitled") for entry in entries]
                         + ["Back"]) - 1
            if index == len(entries):
                continue
            if action == 3:
                entries.pop(index)
                continue
            old = entries[index]
        while True:
            start = number("Start (HH:MM:SS, MM:SS, or seconds)", old.get("startTime", 0))
            stop = old.get("endTime")
            if soundbites and old:
                stop = old["startTime"] + old["duration"]
            end = number("Stop (HH:MM:SS, MM:SS, or seconds)", stop)
            if end <= start:
                error("Stop must be after start.")
                continue
            previous = entries[index - 1] if index else None
            following = entries[index + 1] if index + 1 < len(entries) else None
            previous_end = (previous.get("endTime", previous["startTime"] +
                            previous.get("duration", 0)) if previous else 0)
            if start < previous_end or (following and end > following["startTime"]):
                error("Entries must be chronological and must not overlap.")
                continue
            title = text("Title (maximum 128 characters)" if soundbites else "Title",
                         old.get("title", ""), required=True)
            if soundbites and len(title) > 128:
                error("Soundbite titles must not exceed 128 characters.")
                continue
            entry = dict(old, startTime=start, title=title)
            entry["duration" if soundbites else "endTime"] = end - start if soundbites else end
            if soundbites and "stop" in entry:
                entry["stop"] = end
            if not soundbites:
                entry["img"] = text("Chapter artwork URL (optional HTTPS JPEG/PNG RGB)", old.get("img", ""))
                entry["url"] = text("Chapter link (optional HTTPS)", old.get("url", ""))
            if soundbites and not 15 <= end - start <= 120:
                warning("Recommended soundbite duration is 15 to 120 seconds.")
            if action == 1:
                entries.append(entry)
            else:
                entries[index] = entry
            break


def edit_field(data, field, episode=False):
    current = data.get(field)
    label = field.replace("_", " ").title()
    if field in ("locked", "explicit", "enabled"):
        data[field] = confirm(label, bool(current))
    elif field == "op3":
        console.print("OP3 prefixes each episode's enclosure URL with "
                      "https://op3.dev/e/ to collect podcast metrics.", markup=False)
        data[field] = confirm("Enable OP3 podcast metrics?", bool(current))
    elif field in ("podcast_type", "episode_type"):
        data[field] = choice(label, ["full", "trailer", "bonus"] if episode else
                             ["episodic", "serial"], current)
    elif field in ("audio_preset",):
        data[field] = choice(label, ["standard", "music"], current)
    elif field in ("image_preset",):
        data[field] = choice(label, ["compact", "detail"], current)
    elif field in ("category", "secondary_category", "subcategory"):
        values = CATEGORIES.get(data.get("category"), []) if field == "subcategory" else CATEGORIES
        data[field] = choice(label, values, current, optional=field != "category")
        if field == "category" and data.get("subcategory") not in CATEGORIES.get(data[field], []):
            data["subcategory"] = ""
    elif field == "timezone":
        while True:
            value = text("IANA timezone (e.g. America/New_York)", current or "UTC", True)
            try:
                ZoneInfo(value)
                data[field] = value
                break
            except (KeyError, ValueError):
                error("Unknown IANA timezone.")
    elif field == "artwork_url":
        data[field] = _artwork(current, episode)
    elif field == "podroll":
        data[field] = _podroll(current)
    elif field in ("soundbites", "chapters"):
        data[field] = _segments(current, field == "soundbites")
    elif field in ("length", "duration", "episode_number", "season_number"):
        data[field] = number(label + (" (bytes)" if field == "length" else ""), current,
                             integer=field != "duration",
                             optional=field.endswith("_number"), positive=True)
    elif field == "keywords":
        while True:
            raw = text("Keywords (comma separated, maximum ten)", ", ".join(current or []))
            values = [value.strip() for value in raw.split(",") if value.strip()]
            if len(values) <= 10:
                data[field] = values
                break
            error("At most ten keywords are allowed.")
    else:
        limit = {"title": 60, "description": 4000}.get(field) if episode else None
        while True:
            value = text(label + (f" (maximum {limit} raw characters)" if limit else ""),
                         current, required=field in ("title", "description", "mp3_url",
                                                     "output_dir", "base_url"))
            if not limit or len(value) <= limit:
                data[field] = value
                break
            if field == "title":
                replacement = value[:60].rstrip()
                console.print(f"Original: {value}\nReplacement: {replacement}", markup=False)
                if menu("Overlong title", ["Truncate to first 60 characters", "Edit title"], 2) == 1:
                    data[field] = replacement
                    break
            error(f"Maximum length is {limit} raw characters, including HTML and URLs.")
        if field == "mp3_url":
            if current != data[field]:
                data["length"] = None
                data["duration"] = None
            if confirm("Probe MP3 size and duration?", True):
                try:
                    metadata = probe_media(data[field])
                    for key in ("length", "duration"):
                        value = metadata.get(key)
                        if value is not None and math.isfinite(float(value)) and float(value) > 0:
                            data[key] = int(value) if key == "length" else float(value)
                    if not data.get("length") or not data.get("duration"):
                        warning("Incomplete media metadata. Enter missing values manually.")
                except Exception as exc:
                    warning(f"Media probe failed; enter size and duration manually: {exc}")


def edit_menu(data, fields, episode=False):
    selected = menu("Edit field", [field.replace("_", " ").title() for field in fields]
                    + ["Back"])
    if selected <= len(fields):
        field = fields[selected - 1]
        if field == "hosting":
            hosting_form(data)
            return
        edit_field(data, field, episode)
        if field == "mp3_url":
            missing_media_metadata(data)


def hosting_form(data):
    """Collect nonsecret destination settings; never ask for credentials."""
    from .storage import validate_storage
    selected = menu("Hosting", ["Local web server", "S3-compatible storage"],
                    2 if data.get("hosting") == "s3" else 1)
    data["hosting"] = "s3" if selected == 2 else "local"
    if selected == 2:
        console.print("Configure credentials OUTSIDE Termicast in ~/.s3cfg (s3cmd format). "
                      "Never paste keys here. The publishing/cron account needs the same "
                      "credentials. Use a dedicated show prefix and configure public reads.\n"
                      "Example ~/.s3cfg: access_key, secret_key, host_base, host_bucket.", markup=False)
        for field, label in (("endpoint_url", "HTTPS S3 API endpoint (blank for AWS; e.g. https://us-east-1.linodeobjects.com)"),
                             ("bucket", "Bucket name"),
                             ("prefix", "Show prefix (optional, no outer slashes)"),
                             ("asset_base_url", "Public asset URL of the bucket/prefix (e.g. https://my-bucket.us-east-1.linodeobjects.com/my-show)")):
            data[field] = text(label, data.get(field, ""), required=field in ("bucket", "asset_base_url"))
        data["enabled"] = confirm("Automatically deploy on publish/schedule?", bool(data.get("enabled", False)))
        data["keep_local_media"] = confirm(
            "Keep a local copy of media after it's uploaded to S3 (for redundancy and media-inclusive backups)?",
            bool(data.get("keep_local_media", False)))
    else:
        for field in ("endpoint_url", "bucket", "prefix", "asset_base_url", "enabled", "keep_local_media"):
            data.pop(field, None)
    errors = validate_storage(data)
    if errors:
        raise ValueError("; ".join(errors))


def show_form(show, collect=False):
    """Return validated settings or None; never change an existing identity."""
    data = deepcopy(show)
    if collect:
        for field in SHOW_ESSENTIALS:
            edit_field(data, field)
    while True:
        if collect:
            data["guid"] = new_show(base_url=data["base_url"])["guid"]
        review("Podcast settings", data)
        errors = validate_show(data)
        for message in errors:
            error(message)
        action = menu("Settings review", ["Save", "Edit field", "Cancel"], 2 if errors else 1)
        if action == 3:
            return None
        if action == 2:
            edit_menu(data, SHOW_FIELDS)
        elif not errors:
            return data
        else:
            error("Correct the validation errors before saving.")


def schedule_time(show, *, confirm_time=True):
    zone = show.get("timezone", "UTC")
    while True:
        raw = text(f"Publication time in {zone} (ISO date/time; explicit offset resolves DST; - cancels)",
                   example="2027-06-15T09:00:00 (local time) or 2027-06-15T09:00:00+00:00 (explicit UTC offset); choose a future date")
        if not raw:
            return None
        try:
            when = local_to_utc(raw, zone)
            if when <= datetime.now(timezone.utc):
                raise ValueError("Schedule a time in the future, or use Publish now.")
            console.print(f"Local: {when.astimezone(ZoneInfo(zone)).isoformat()} ({zone})\n"
                          f"UTC:   {when.astimezone(timezone.utc).isoformat()}", markup=False)
            if not confirm_time or confirm("Use this publication time?", True):
                return when
        except (ValueError, KeyError) as exc:
            error(exc)


def optional_assets(episode, show):
    """Keep additions in the review record until the episode is saved."""
    from .assets import read_asset, read_chapters, check_vtt
    from .models import chapters_relative

    action = menu("Optional assets", ["Chapters JSON (local path or HTTPS URL)",
                                      "Chapters manually", "Transcript VTT (local path or HTTPS URL)", "Back"], 4)
    try:
        if action == 1:
            value = read_chapters(text("Chapter JSON path or HTTPS URL", required=True), episode)
            review("Chapter replacement", {"chapters": value})
            if confirm("Use these chapters?", False):
                episode["chapters"] = value
                episode.setdefault("_replace_fields", []).append("chapters")
        elif action == 2:
            value = _segments(episode.get("chapters", []), False)
            if value != episode.get("chapters", []):
                episode["chapters"] = value
                episode.setdefault("_replace_fields", []).append("chapters")
        elif action == 3:
            value = check_vtt(read_asset(text("VTT path or HTTPS URL", required=True)))
            from .storage import asset_base, asset_root
            filename = chapters_relative(episode).split("/", 1)[1]
            url = asset_base(show) + "/transcripts/" + filename
            console.print(f"On save: {asset_root(show)}/transcripts/{filename}\nPublic URL: {url}", markup=False)
            if confirm("Save this transcript with the episode?", False):
                episode.update(transcript_url=url, _transcript_vtt=value)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        error(f"Asset was not added: {exc}")


def title_for_save(episode):
    if len(episode["title"]) <= 60:
        return
    original = episode["title"]
    replacement = original[:60].rstrip()
    console.print(f"Original: {original}\nReplacement: {replacement}", markup=False)
    if menu("Overlong title", ["Confirm first 60 characters", "Edit title"], 2) == 1:
        episode["title"] = replacement
    else:
        edit_field(episode, "title", episode=True)


def check_slug_collision(db, show_id, slug, exclude_guid=None):
    for episode in db.list_episodes(show_id):
        if episode["guid"] == exclude_guid:
            continue
        if episode.get("slug") == slug:
            raise ValueError(f"Slug '{slug}' is already used by episode {episode['guid']}; choose another name")


def _choose_slug(db, show, episode, explicit_slug):
    if explicit_slug is not None:
        reason = slug_error(explicit_slug)
        if reason:
            raise ValueError(f"Invalid slug: {reason}")
        check_slug_collision(db, show["id"], explicit_slug)
        return explicit_slug
    suggested = suggest_slug(episode)
    console.print(f"Suggested slug: {suggested}", style=ACCENT, markup=False)
    while True:
        value = text("Slug (letters, numbers, hyphens, underscores)", suggested, required=True)
        reason = slug_error(value)
        if reason:
            error(f"Invalid slug: {reason}")
            continue
        try:
            check_slug_collision(db, show["id"], value)
            return value
        except ValueError as exc:
            error(exc)


def add_episode(db, publisher, show, files, *, slug=None, audio_preset=None, image_preset=None,
                keep_audio=False, keep_image=False):
    """Create an episode from local files: prepare media, collect details, publish.

    This is the shared implementation behind `termicast add` and the menu's
    Add episode action. Audio is required; artwork and transcript are optional.
    """
    from .media import identify_files, prepare_media
    from .storage import asset_base

    roles = identify_files(files)
    audio_preset = audio_preset or show.get("audio_preset", "standard")
    image_preset = image_preset or show.get("image_preset", "compact")

    episode = new_episode(explicit=show.get("explicit", False))
    if slug is None:
        episode["season_number"] = number("Season number (optional)", None, integer=True, optional=True, positive=True)
        episode["episode_number"] = number("Episode number (optional)", None, integer=True, optional=True, positive=True)
    chosen_slug = _choose_slug(db, show, episode, slug)
    show_export_notice()

    prepared = prepare_media(show, roles, slug=chosen_slug, audio_preset=audio_preset,
                             image_preset=image_preset, keep_audio=keep_audio,
                             keep_image=keep_image, progress=True)
    review("Prepared media", prepared.review())
    episode.update(slug=chosen_slug)
    episode["mp3_url"] = asset_base(show) + "/" + prepared.audio_relative
    episode["length"] = prepared.audio_after
    episode["duration"] = prepared.audio_meta["duration"]
    episode["audio_path"] = prepared.audio_relative
    if prepared.image_relative:
        episode["artwork_url"] = asset_base(show) + "/" + prepared.image_relative
        episode["image_path"] = prepared.image_relative
    if prepared.transcript_relative:
        episode["transcript_url"] = asset_base(show) + "/" + prepared.transcript_relative
        episode["transcript_path"] = prepared.transcript_relative

    for field in ("title", "description"):
        edit_field(episode, field, episode=True)

    while True:
        review("Episode review", episode)
        errors = validate_episode(episode)
        for message in errors:
            error(message)
        action = menu("Episode review", ["Edit details / more options", "Publish now", "Schedule",
                                         "Cancel", "Add chapters / transcript"], 1)
        if action == 5:
            optional_assets(episode, show)
            continue
        if action == 4:
            return
        if action == 1:
            edit_menu(episode, EPISODE_DETAIL_FIELDS, episode=True)
            continue
        title_for_save(episode)
        errors = validate_episode(episode)
        if errors:
            error("Correct the validation errors before publishing or scheduling.")
            continue
        when = schedule_time(show, confirm_time=False) if action == 3 else None
        if action == 3 and when is None:
            continue
        if not confirm("Schedule this episode?" if when else "Publish this episode now?"):
            continue
        try:
            guid = publisher.publish(show["id"], episode, when=when)
        except Exception as exc:
            error(f"Publication failed: {exc}. Your episode remains in this review.")
            continue
        console.print(f"{'Scheduled' if when else 'Published'} episode {guid}",
                      style=ACCENT, markup=False)
        return


def episode_form(show, publisher, db):
    """Menu Add episode: prompt for local files, then the shared file flow."""
    console.print("Add episode from local files. Local files must be on this machine. "
                  "Audio is required; artwork and transcript are optional and identified by type.",
                  markup=False)
    audio = text("Audio file path (mp3, wav, flac, m4a, ogg, opus)", required=True)
    paths = [audio]
    if confirm("Add cover artwork file?", False):
        paths.append(text("Artwork file path (JPEG/PNG)", required=True))
    if confirm("Add a WebVTT transcript file?", False):
        paths.append(text("Transcript file path (.vtt)", required=True))
    add_episode(db, publisher, show, paths)


def edit_episode_form(show, publisher, saved):
    episode = deepcopy(saved)
    while True:
        review("Edit episode (GUID is permanent)", episode)
        options = ["Edit field", "Save", "Cancel"]
        if saved["status"] != "published":
            options += ["Reschedule", "Publish now"]
        options += ["Add optional assets"]
        action = menu("Episode editor", options, 1)
        if action == len(options):
            optional_assets(episode, show)
            continue
        if action == 3:
            return
        if action == 1:
            edit_menu(episode, EPISODE_FIELDS, episode=True)
            continue
        if action == 4:
            when = schedule_time(show, confirm_time=False)
            if when:
                episode.update(publish_at=when.isoformat(), status="scheduled")
            continue
        if action == 5:
            episode.update(publish_at=datetime.now(timezone.utc).isoformat(), status="published")
        title_for_save(episode)
        if not confirm("Save episode changes?", True):
            continue
        try:
            def checked(existing, current_show):
                if existing.get(saved["guid"]) != saved:
                    raise ValueError("Episode changed since this editor opened; reopen it before saving")
                return [episode]
            publisher.merge(show["id"], checked)
        except Exception as exc:
            error(f"Save failed: {exc}. Persisted intent may need publish-due recovery.")
            continue
        console.print("Episode saved.", style=ACCENT)
        return


def hosting_menu(db, publisher, show):
    """Hosting submenu: setup, deploy, checks, and guidance."""
    from .hosting import doctor, nginx_snippet, apache_snippet
    while True:
        action = menu("Hosting", ["Configure hosting", "Deploy", "Deploy (dry run)",
                                  "Hosting checks (doctor)", "Nginx MIME snippet",
                                  "Apache MIME snippet", "Migration guidance", "Back"])
        try:
            if action == 8:
                return
            if action == 1:
                data = dict(show)
                hosting_form(data)
                db.save_show(data)
                publisher.regenerate(show["id"])
                show = data
                console.print("Hosting settings saved.", style=ACCENT)
            elif action == 2:
                publisher.deploy(show["id"])
                console.print("Deployed.", style=ACCENT)
            elif action == 3:
                publisher.deploy(show["id"], dry_run=True)
                console.print("Dry run complete: no uploads or bucket probes were made.", style=ACCENT)
            elif action == 4:
                problems = doctor(db, show["id"])
                if problems:
                    for problem in problems:
                        warning(problem)
                else:
                    console.print("No hosting problems found.", style=ACCENT)
            elif action == 5:
                console.print(nginx_snippet(show), markup=False)
            elif action == 6:
                console.print(apache_snippet(show), markup=False)
            elif action == 7:
                from .migration import migration_guidance
                console.print(migration_guidance(show), markup=False)
        except ExitRequested:
            raise
        except Exception as exc:
            error(f"Hosting action failed: {exc}")
