"""Private, consistent snapshots of saved state and generated podcast files."""

from contextlib import ExitStack, nullcontext
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from .database import filesystem_lock
from .models import chapters_relative, transcript_relative
from .publisher import fsync_dir
from .storage import asset_root


def create_backup(db, destination=None, include_media=False, *, _locked=False):
    """Return a new ZIP path; missing public files are recorded in its manifest.

    Hold the same locks as publication until all files have been copied. Backups
    include saved state only, not in-memory forms or externally hosted assets.

    `_locked` is no longer load-bearing: `filesystem_lock` is re-entrant within
    a thread, so a caller that already holds the database lock can call this
    without it. It is kept because it documents the caller's intent and avoids
    a pointless depth bump.
    """
    destination = Path(destination or db.path.parent / "backups").expanduser().resolve()
    now = datetime.now(timezone.utc)
    name = f"termicast-{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex[:8]}.zip"
    with (nullcontext() if _locked else db.lock()), ExitStack() as locks:
        shows = db.list_shows()
        for show in shows:
            assets = asset_root(show)
            if destination.is_relative_to(assets.resolve()):
                raise ValueError("Backups must be outside every podcast's public asset directory")
            if assets.is_symlink():
                raise ValueError("Refusing symbolic-link asset directory")
            output = Path(show["output_dir"])
            if destination.is_relative_to(output.resolve()):
                raise ValueError("Backups must be outside every podcast's public output directory")
            if output.is_symlink():
                raise ValueError(f"Refusing symbolic-link output directory: {output}")
            if output.exists():
                locks.enter_context(filesystem_lock(output / ".termicast.lock"))
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        manifest = {"version": 1, "created_at": now.isoformat(), "database": "termicast.db",
                    "source_database": str(db.path), "shows": [], "missing_files": [],
                    "include_media": include_media,
                    "excludes": ["unsaved forms", "externally hosted assets"] + ([] if include_media else ["local media"])}
        with tempfile.TemporaryDirectory(prefix=".termicast-backup-", dir=destination) as staging:
            snapshot = Path(staging) / "termicast.db"
            target = sqlite3.connect(snapshot)
            try:
                with db.connection() as source:
                    source.backup(target)
            finally:
                target.close()
            archive_path = Path(staging) / name
            with archive_path.open("xb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                with ZipFile(handle, "w", compression=ZIP_DEFLATED) as archive:
                    archive.write(snapshot, "termicast.db")
                    for show in shows:
                        output = Path(show["output_dir"])
                        prefix = f"outputs/{show['id']}"
                        manifest["shows"].append({"id": show["id"], "title": show["title"],
                                                  "output_dir": str(output), "archive_dir": prefix})
                        assets = asset_root(show)
                        chapters = assets / "chapters"
                        if chapters.is_symlink():
                            raise ValueError(f"Refusing symbolic-link chapters directory: {chapters}")
                        files = [output / "feed.xml"]
                        files.extend(sorted(chapters.glob("*.json")))
                        if include_media:
                            for folder in ("audio", "images", "transcripts"):
                                root = assets / folder
                                if root.is_symlink():
                                    raise ValueError(f"Refusing symbolic-link media directory: {root}")
                                for directory, dirs, names in os.walk(root, followlinks=False):
                                    for name in dirs + names:
                                        path = Path(directory) / name
                                        if path.is_symlink():
                                            raise ValueError(f"Refusing symbolic-link media: {path}")
                                    dirs[:] = [name for name in dirs if not name.startswith(".")]
                                    files.extend(Path(directory) / name for name in names if not name.startswith("."))
                        for episode in db.list_episodes(show["id"]):
                            if episode["status"] == "published" and episode.get("chapters"):
                                expected = assets / chapters_relative(episode)
                                if expected not in files:
                                    files.append(expected)
                            if episode.get("_transcript_vtt"):
                                expected = assets / transcript_relative(episode)
                                if expected not in files:
                                    files.append(expected)
                        for path in files:
                            if path.is_symlink():
                                raise ValueError(f"Refusing symbolic-link output file: {path}")
                            if not path.exists():
                                manifest["missing_files"].append(str(path))
                                continue
                            if not path.is_file():
                                raise ValueError(f"Expected a regular output file: {path}")
                            relative = "feed.xml" if path == output / "feed.xml" else path.relative_to(assets).as_posix()
                            archive.write(path, f"{prefix}/{relative}")
                    archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            result = destination / name
            os.link(archive_path, result)
            fsync_dir(destination)
    return result
