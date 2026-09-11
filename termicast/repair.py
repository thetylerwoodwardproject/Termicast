"""Explicit, backed-up repairs; scanning never downloads or changes outputs."""

from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .backup import create_backup
from .feed import _parse_xml, _tag, render_feed, validate_feed
from .models import chapters_relative, transcript_relative
from .publisher import Publisher
from .database import filesystem_lock
from .validation import validate_episode
from .storage import asset_root, asset_base


def scan_show(db, show_id):
    with db.lock():
        show = db.get_show(show_id)
        if show is None:
            raise ValueError("Unknown podcast ID")
        episodes = db.list_episodes(show_id)
        with db.connection() as conn:
            template = conn.execute("SELECT template FROM shows WHERE id=?", (show_id,)).fetchone()[0]
    output = Path(show["output_dir"])
    fixes, issues, missing = [], [], []
    for episode in episodes:
        issues.extend(f"{episode['guid']}: {error}" for error in validate_episode(episode))
        title = episode["title"]
        if len(title) > 60:
            fixes.append({"guid": episode["guid"], "original": title, "replacement": title[:60].rstrip()})
    try:
        issues.extend(validate_feed((output / "feed.xml").read_bytes()))
    except OSError as exc:
        issues.append(f"Feed needs regeneration: {exc}")
    published = [e for e in episodes if e["status"] == "published"]
    assets = asset_root(show)
    regenerable = {str(assets / chapters_relative(e)) for e in published if e.get("chapters")}
    regenerable.update(str(assets / transcript_relative(e)) for e in published if e.get("_transcript_vtt"))
    try:
        root = _parse_xml(render_feed(show, template, published, datetime.now(timezone.utc),
                                     excluded_guids={e["guid"] for e in episodes if e["status"] != "published"}))
    except (ValueError, TypeError, KeyError) as exc:
        issues.append(f"Cannot regenerate from saved data: {exc}")
        return {"show": show, "episodes": episodes, "template": template, "fixes": fixes,
                "issues": issues, "missing": missing}
    base = urlsplit(asset_base(show) + "/")
    for element in root.iter():
        if element.tag not in ("enclosure", _tag("podcast:chapters"), _tag("podcast:transcript"), _tag("itunes:image")):
            continue
        url = element.get("url") or element.get("href") or ""
        parts = urlsplit(url)
        if (parts.scheme, parts.netloc) != (base.scheme, base.netloc) or not parts.path.startswith(base.path):
            continue
        relative = Path(unquote(parts.path[len(base.path):]))
        path = assets / relative
        if not path.resolve().is_relative_to(assets.resolve()):
            issues.append(f"Unsafe local resource path: {url}")
        elif not path.is_file():
            issues.append(f"Referenced local resource missing: {path}")
            missing.append({"path": str(path), "url": url, "regenerable": str(path) in regenerable})
            if str(path) not in regenerable:
                issues.append(f"Not recoverable from stored data; supply a local file or correct its URL: {url}")
        elif relative.parts and relative.parts[0] not in ("audio", "chapters", "images", "transcripts"):
            issues.append(f"Referenced resource outside standard media directories: {path}")
    for episode in published:
        if episode.get("chapters") and not (assets / chapters_relative(episode)).is_file():
            issues.append(f"Saved chapters can be regenerated: {episode['guid']}")
    return {"show": show, "episodes": episodes, "template": template, "fixes": fixes,
            "issues": issues, "missing": missing}


def repair_show(db, scan, selected, recoveries=None, output_dir=None):
    """Apply selected previewed title fixes and rebuild only published content."""
    show = scan["show"]
    selected = set(selected)
    recoveries = recoveries or {}
    allowed = {entry["path"] for entry in scan["missing"]}
    if set(recoveries) - allowed:
        raise ValueError("Recovery destination was not in the scan")
    output = Path(output_dir or show["output_dir"]).expanduser().absolute()
    if output_dir and output.exists():
        raise ValueError("Corrected output directory must be new; existing files are not moved")
    if selected - {f["guid"] for f in scan["fixes"]}:
        raise ValueError("Unknown repair selection")
    with db.lock():
        if db.get_show(show["id"]) != show or db.list_episodes(show["id"]) != scan["episodes"]:
            raise ValueError("Podcast changed since scan; scan again before repairing")
        with db.connection() as conn:
            template = conn.execute("SELECT template FROM shows WHERE id=?", (show["id"],)).fetchone()[0]
        if template != scan["template"]:
            raise ValueError("Source template changed; scan again")
        backup = create_backup(db, _locked=True)
        with db.connection() as conn:
            for old in scan["episodes"]:
                if old["guid"] not in selected:
                    continue
                episode = dict(old)
                status, target = episode.pop("status"), episode.pop("publish_at")
                episode["title"] = episode["title"][:60].rstrip()
                conn.execute("""INSERT INTO episodes VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(show_id, guid) DO UPDATE SET data=excluded.data""",
                    (episode["guid"], show["id"], json.dumps(episode), target, status))
            conn.execute("UPDATE shows SET dirty=1 WHERE id=?", (show["id"],))
            if output_dir:
                updated = dict(show, output_dir=str(output))
                conn.execute("UPDATE shows SET settings=? WHERE id=?", (json.dumps(updated), show["id"]))
        output.mkdir(parents=True, exist_ok=True)
        with filesystem_lock(output / ".termicast.lock"):
            for destination, source in recoveries.items():
                relative = Path(destination).relative_to(asset_root(show))
                target = asset_root(show) / relative
                if not target.resolve().is_relative_to(asset_root(show).resolve()) or target.exists():
                    raise ValueError(f"Recovery destination is unsafe or no longer missing: {target}")
                import os
                import shutil
                import tempfile
                target.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix=".repair-", dir=target.parent)
                try:
                    with os.fdopen(fd, "wb") as handle, Path(source).expanduser().open("rb") as original:
                        shutil.copyfileobj(original, handle)
                        handle.flush()
                        os.fchmod(handle.fileno(), 0o644)
                        os.fsync(handle.fileno())
                    os.replace(temporary, target)
                    directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                finally:
                    Path(temporary).unlink(missing_ok=True)
        Publisher(db)._write(show["id"], datetime.now(timezone.utc), include_due=False)
    return backup, scan_show(db, show["id"])
