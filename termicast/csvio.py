"""Round-trippable episode CSV, merged only after whole-file preflight."""

import csv
from datetime import datetime
import io
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import new_episode
from .publisher import Publisher, atomic_write
from .validation import local_to_utc

FIELDS = tuple(new_episode()) + ("published_at", "timezone", "status")
JSON_FIELDS = ("keywords", "chapters", "soundbites")


def export_csv(db, show_id, path, selection="all"):
    if selection not in ("all", "published", "scheduled"):
        raise ValueError("Filter must be all, published, or scheduled")
    with db.lock():
        show = db.get_show(show_id)
        if show is None:
            raise ValueError("Unknown podcast ID")
        episodes = db.list_episodes(show_id)
        shows = db.list_shows()
    target = Path(path).expanduser().resolve()
    if target in (db.path, Path(str(db.path) + ".lock")):
        raise ValueError("CSV export cannot overwrite database state or its lock")
    if any(target.is_relative_to(Path(s["output_dir"]).resolve()) for s in shows):
        raise ValueError("CSV exports must stay outside public output directories")
    content = io.StringIO(newline="")
    writer = csv.DictWriter(content, fieldnames=FIELDS)
    writer.writeheader()
    for episode in episodes:
        if selection != "all" and episode["status"] != selection:
            continue
        row = {key: episode.get(key, "") for key in FIELDS}
        row["timezone"] = show["timezone"]
        row["published_at"] = datetime.fromisoformat(episode["publish_at"]).astimezone(ZoneInfo(show["timezone"])).isoformat()
        for key in JSON_FIELDS:
            row[key] = json.dumps(episode.get(key, []), ensure_ascii=True, allow_nan=False)
        writer.writerow(row)
    atomic_write(target, content.getvalue().encode("utf-8"), mode=0o600)
    return target


def import_csv(db, show_id, path, review_titles=None):
    with Path(path).expanduser().open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        if not headers or len(headers) != len(set(headers)) or set(headers) - set(FIELDS):
            raise ValueError("CSV requires unique supported column names")
        rows = list(reader)

    def preflight(existing, show):
        prepared = []
        for number, row in enumerate(rows, 2):
            try:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("Wrong number of CSV columns")
                guid = row.get("guid", "")
                if not guid:
                    matches = [e for e in existing.values() if e["mp3_url"] == row.get("mp3_url", "")]
                    if len(matches) > 1:
                        raise ValueError("Ambiguous MP3 URL; supply an explicit GUID")
                    guid = matches[0]["guid"] if matches else new_episode()["guid"]
                old = existing.get(guid)
                episode = dict(old) if old else new_episode(guid=guid)
                for key, value in row.items():
                    if key in ("guid", "timezone", "published_at", "status"):
                        continue
                    if key in JSON_FIELDS:
                        value = json.loads(value) if value else []
                    elif key in ("length", "episode_number", "season_number"):
                        value = int(value) if value else None
                    elif key == "duration":
                        value = float(value) if value else None
                    elif key == "explicit":
                        if value.lower() not in ("true", "false", "1", "0", ""):
                            raise ValueError("explicit must be true or false")
                        value = value.lower() in ("true", "1")
                    episode[key] = value
                if row.get("published_at"):
                    episode["published_at"] = local_to_utc(row["published_at"], row.get("timezone") or show["timezone"]).isoformat()
                    episode["publish_at"] = episode["published_at"]
                if row.get("status"):
                    episode["status"] = row["status"]
                prepared.append(episode)
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError(f"CSV row {number}: {exc}") from exc
        changes = [(e["guid"], e["title"], e["title"][:60].rstrip())
                   for e in prepared if len(e["title"]) > 60]
        if changes:
            if review_titles is None or not review_titles(changes):
                raise ValueError("Overlong titles require original/replacement review and confirmation")
            for episode in prepared:
                if len(episode["title"]) > 60:
                    episode["title"] = episode["title"][:60].rstrip()
        return prepared

    return Publisher(db).merge(show_id, preflight)
