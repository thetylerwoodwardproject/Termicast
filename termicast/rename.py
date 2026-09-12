"""Rename one episode's managed assets: plan first, move atomically, commit once.

Renaming touches three things that must agree afterwards: the files under the
asset root, the episode record's paths and URLs, and `import_url_map`, which
`render_feed` re-applies to the retained template on every render. The plan is
built without writing anything so the caller can show the URL changes and ask
for confirmation; applying it moves the files inside one database transaction,
so a failed move leaves the record untouched.
"""

import json
import os
from pathlib import Path

from .models import chapters_relative, slug_error, transcript_relative
from .publisher import fsync_dir, operation_lock, output_lock
from .storage import asset_base, asset_root, local_relative
from .validation import validate_episode

ASSET_ROLES = (("mp3_url", "audio_path"), ("artwork_url", "image_path"),
               ("transcript_url", "transcript_path"))
# The managed asset folders, matching the set repair.scan_show enforces.
MANAGED_FOLDERS = ("audio", "chapters", "images", "transcripts")


class RenamePlan:
    """What renaming one episode would do. Built without touching anything."""

    def __init__(self, slug):
        self.slug = slug
        self.moves = []          # [(old_relative, new_relative)] local file moves
        self.remote_moves = []   # [(old_relative, new_relative)] S3-only, no local file
        self.fields = {}         # episode field updates
        self.url_changes = []    # [(old_public_url, new_public_url)]
        self.map_updates = {}    # import_url_map key -> new value
        self.orphans = []        # files left behind at the old name
        self.missing = []        # referenced assets with no local file (non-S3 hosting)
        self.blocked = []        # destinations already occupied
        self.warnings = []

    def __bool__(self):
        return bool(self.moves or self.remote_moves or self.orphans)


def _safe_relative(assets, relative):
    """Reject anything outside the managed folders or outside the asset root."""
    parts = Path(relative).parts
    if not parts or parts[0] not in MANAGED_FOLDERS:
        return False
    return (assets / relative).resolve().is_relative_to(assets.resolve())


def plan_rename(show, episode, new_slug, others=()):
    """Describe renaming `episode` to `new_slug`. Writes nothing.

    `others` are the show's remaining episodes, used to leave an asset alone
    when a second episode points at the same file.
    """
    reason = slug_error(new_slug)
    if reason:
        raise ValueError(f"Invalid slug: {reason}")
    assets = asset_root(show)
    plan = RenamePlan(new_slug)
    shared = set()
    for other in others:
        if other.get("guid") == episode.get("guid"):
            continue
        for field, path_field in ASSET_ROLES:
            relative = other.get(path_field) or local_relative(show, other.get(field))
            if relative:
                shared.add(str(relative))

    for field, path_field in ASSET_ROLES:
        url = episode.get(field)
        relative = episode.get(path_field) or local_relative(show, url)
        if not relative:
            continue
        relative = str(relative)
        if not _safe_relative(assets, relative):
            plan.warnings.append(f"Left alone, outside the managed folders: {relative}")
            continue
        if relative in shared:
            plan.warnings.append(f"Left alone, shared with another episode: {relative}")
            continue
        source = assets / relative
        target = source.with_name(new_slug + source.suffix)
        new_relative = target.relative_to(assets).as_posix()
        if new_relative == relative:
            continue
        if not source.is_file():
            if show.get("hosting") == "s3":
                # No local copy (S3 without "keep a local copy of media"):
                # the object is renamed directly in S3 instead of moving a
                # local file, so this isn't actually missing anything.
                plan.remote_moves.append((relative, new_relative))
            else:
                plan.missing.append(relative)
                continue
        elif target.exists():
            plan.blocked.append(new_relative)
            continue
        else:
            plan.moves.append((relative, new_relative))
        plan.fields[path_field] = new_relative
        new_url = asset_base(show) + "/" + new_relative
        plan.fields[field] = new_url
        if url:
            plan.url_changes.append((url, new_url))
            for key, value in (show.get("import_url_map") or {}).items():
                if value == url:
                    plan.map_updates[key] = new_url

    # Chapter JSON and the managed transcript carry no stored path: they are
    # recomputed from the slug, so the new files appear on the next write and
    # only the old ones need clearing up.
    renamed = dict(episode, slug=new_slug)
    for relative_for, present in ((chapters_relative, bool(episode.get("chapters"))),
                                  (transcript_relative, bool(episode.get("_transcript_vtt")))):
        if not present:
            continue
        old_relative, new_relative = relative_for(episode), relative_for(renamed)
        if old_relative == new_relative:
            continue
        if (assets / old_relative).is_file():
            plan.orphans.append(old_relative)
        if (assets / new_relative).exists():
            plan.blocked.append(new_relative)

    plan.fields["slug"] = new_slug
    return plan


def _apply_moves(assets, moves):
    """Move every file, undoing the moves already made if one fails."""
    done = []
    try:
        for old, new in moves:
            target = assets / new
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(assets / old, target)
            done.append((old, new))
        for directory in {(assets / new).parent for _, new in moves}:
            fsync_dir(directory)
    except OSError:
        for old, new in reversed(done):
            try:
                os.replace(assets / new, assets / old)
            except OSError:
                pass
        raise


def _apply_remote_moves(show, moves):
    """Rename S3 objects in place, undoing whatever succeeded if one fails."""
    from .s3deploy import remote_rename
    done = []
    try:
        for old, new in moves:
            remote_rename(show, old, new)
            done.append((old, new))
    except Exception:
        for old, new in reversed(done):
            try:
                remote_rename(show, new, old)
            except Exception:
                pass
        raise


def rename_episode(db, show, episode, plan):
    """Commit a planned rename. Returns the updated (show, episode).

    The caller regenerates the feed afterwards so the publisher takes its own
    locks. Locks are taken operation -> database -> output, matching
    `publisher._write`, so a concurrent publish of the same show serializes
    against this rather than deadlocking with it.
    """
    if plan.missing:
        raise ValueError("No local file to move: " + ", ".join(plan.missing))
    if plan.blocked:
        raise ValueError("Destination already exists: " + ", ".join(plan.blocked))
    if not plan.moves and not plan.remote_moves:
        raise ValueError("This episode has no managed local files to rename")
    updated = dict(episode, **plan.fields)
    status, target = updated.pop("status"), updated.pop("publish_at")
    errors = validate_episode(updated)
    if errors:
        raise ValueError("; ".join(errors))
    assets = asset_root(show)
    with operation_lock(show), db.lock():
        current = db.get_show(show["id"])
        if current is None:
            raise ValueError("Unknown podcast ID")
        live = {e["guid"]: e for e in db.list_episodes(show["id"])}
        if live.get(episode["guid"]) != episode:
            raise ValueError("Episode changed since the rename was planned; reopen it")
        for guid, other in live.items():
            if guid != episode["guid"] and other.get("slug") == plan.slug:
                raise ValueError(f"Slug '{plan.slug}' is already used by episode {guid}")
        settings = dict(current)
        if plan.map_updates:
            settings["import_url_map"] = {**(current.get("import_url_map") or {}),
                                          **plan.map_updates}
        if settings.get("asset_files"):
            moved = dict(plan.moves) | dict(plan.remote_moves)
            settings["asset_files"] = sorted({moved.get(f, f) for f in settings["asset_files"]})
        with db.connection() as conn:
            conn.execute("UPDATE shows SET settings=?, dirty=1 WHERE id=?",
                         (json.dumps(settings), show["id"]))
            conn.execute("""INSERT INTO episodes VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(show_id, guid) DO UPDATE SET data=excluded.data""",
                         (updated["guid"], show["id"],
                          json.dumps(updated, allow_nan=False), target, status))
            with output_lock(settings):
                _apply_moves(assets, plan.moves)
                try:
                    if plan.remote_moves:
                        _apply_remote_moves(show, plan.remote_moves)
                except Exception:
                    # The DB transaction is about to roll back; put local
                    # files back the way _apply_moves left them.
                    _apply_moves(assets, [(new, old) for old, new in plan.moves])
                    raise
    return settings, dict(updated, status=status, publish_at=target)


def clear_orphans(show, plan):
    """Delete the chapter JSON and transcript left behind at the old name."""
    assets = asset_root(show)
    removed = []
    for relative in plan.orphans:
        path = assets / relative
        if _safe_relative(assets, relative) and path.is_file():
            path.unlink()
            removed.append(relative)
    return removed
