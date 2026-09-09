"""Recoverable publication: durable intent, chapter files, feed, then state."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from .database import filesystem_lock
from .feed import render_feed
from .validation import validate_episode
from .models import chapter_filename


def atomic_write(path, data, mode=0o644):
    """Replace a complete file on the same filesystem and fsync its directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Publisher:
    def __init__(self, db):
        self.db = db

    def publish(self, show_id, episode, when=None):
        errors = validate_episode(episode)
        if errors:
            raise ValueError("; ".join(errors))
        now = datetime.now(timezone.utc)
        target = when if when is not None else now
        if target.tzinfo is None or target.utcoffset() is None:
            raise ValueError("Publication time must include a timezone")
        target = target.astimezone(timezone.utc).isoformat()
        episode = dict(episode, published_at=target)
        with self.db.lock():
            if self.db.get_show(show_id) is None:
                raise ValueError("Unknown podcast ID")
            with self.db.connection() as conn:
                existing = conn.execute("SELECT * FROM episodes WHERE show_id=? AND guid = ?", (show_id, episode["guid"])).fetchone()
                if existing:
                    saved = json.loads(existing["data"])
                    comparable = dict(episode, published_at=saved["published_at"])
                    if existing["show_id"] != show_id or saved != comparable or (when is not None and existing["publish_at"] != target):
                        raise ValueError("Episode GUID already exists with different content or schedule")
                else:
                    conn.execute("INSERT INTO episodes VALUES (?, ?, ?, ?, 'scheduled')",
                                 (episode["guid"], show_id, json.dumps(episode), target))
            if when is None or datetime.fromisoformat(target) <= now:
                self._write(show_id, now, include_due=True)
        return episode["guid"]

    def regenerate(self, show_id):
        with self.db.lock():
            self._write(show_id, datetime.now(timezone.utc), include_due=False)

    def merge(self, show_id, episodes):
        """Preflight a complete batch under the writer lock, then persist intent.

        A failed file replacement leaves every row and the dirty flag committed;
        publish-due can recover without replaying a partially applied CSV.
        """
        with self.db.lock():
            show = self.db.get_show(show_id)
            if show is None:
                raise ValueError("Unknown podcast ID")
            existing = {e["guid"]: e for e in self.db.list_episodes(show_id)}
            if callable(episodes):
                episodes = episodes(existing, show)
            prepared, seen, releases = [], set(), set()
            now = datetime.now(timezone.utc)
            for episode in episodes:
                episode = dict(episode)
                errors = validate_episode(episode)
                guid = episode.get("guid")
                if guid in seen:
                    errors.append("Duplicate GUID in batch")
                seen.add(guid)
                old = existing.get(guid)
                target = datetime.fromisoformat(episode.get("publish_at") or episode.get("published_at") or now.isoformat())
                if target.tzinfo is None:
                    errors.append("Publication time needs a timezone")
                target = target.astimezone(timezone.utc).isoformat()
                status = episode.get("status", "scheduled" if target > now.isoformat() else "published")
                if status not in ("published", "scheduled"):
                    errors.append("Status must be published or scheduled")
                if old and old["status"] == "published" and (target != old["publish_at"] or status != "published"):
                    errors.append("Published episodes cannot be rescheduled or unpublished")
                if status == "published" and target > now.isoformat():
                    errors.append("Future publication must be scheduled")
                if errors:
                    raise ValueError(f"Episode {guid}: " + "; ".join(errors))
                episode.pop("status", None)
                episode.pop("publish_at", None)
                episode["published_at"] = target
                if status == "published" and (not old or old["status"] != "published"):
                    releases.add(guid)
                prepared.append((guid, show_id, json.dumps(episode, allow_nan=False), target, status))
            # Render before committing to catch XML/serialization failures too.
            with self.db.connection() as conn:
                template = conn.execute("SELECT template FROM shows WHERE id=?", (show_id,)).fetchone()[0]
            preview = {guid: e for guid, e in existing.items() if e["status"] == "published"}
            for guid, _, data, _, status in prepared:
                # Scheduled rows must also be renderable before durable intent.
                preview[guid] = json.loads(data)
            render_feed(show, template, list(preview.values()), now)
            with self.db.connection() as conn:
                conn.executemany("""INSERT INTO episodes VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(show_id, guid) DO UPDATE SET data=excluded.data,
                    publish_at=excluded.publish_at, status=excluded.status""",
                    [(guid, sid, data, target, "scheduled" if guid in releases else status)
                     for guid, sid, data, target, status in prepared])
                conn.execute("UPDATE shows SET dirty=1 WHERE id=?", (show_id,))
            self._write(show_id, now, include_due=False, release_guids=releases)
        return len(prepared)

    def publish_due(self):
        now = datetime.now(timezone.utc)
        count = 0
        failures = []
        with self.db.lock():
            with self.db.connection() as conn:
                ids = [row[0] for row in conn.execute("""
                    SELECT id FROM shows WHERE dirty=1 OR id IN
                    (SELECT show_id FROM episodes WHERE status='scheduled' AND publish_at<=?)
                    """, (now.isoformat(),))]
            for show_id in ids:
                try:
                    count += self._write(show_id, now, include_due=True)
                except Exception as exc:
                    failures.append(f"{show_id}: {exc}")
        if failures:
            raise RuntimeError(f"Published {count} episode(s); retry failed podcasts: " + "; ".join(failures))
        return count

    def _write(self, show_id, now, include_due, release_guids=()):
        # The caller holds the database lock across snapshots, files, and commit.
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM shows WHERE id=?", (show_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown podcast ID")
            show = json.loads(row["settings"])
            rows = conn.execute("SELECT * FROM episodes WHERE show_id=? ORDER BY publish_at DESC", (show_id,)).fetchall()
        selected = [r for r in rows if r["status"] == "published" or
                    ((include_due or r["guid"] in release_guids) and r["publish_at"] <= now.isoformat())]
        episodes = [json.loads(r["data"]) for r in selected]
        selected_guids = {r["guid"] for r in selected}
        data = render_feed(show, row["template"], episodes, now,
                           excluded_guids={r["guid"] for r in rows} - selected_guids)
        output = Path(show["output_dir"])
        output.mkdir(parents=True, exist_ok=True)
        with filesystem_lock(output / ".termicast.lock"):
            for episode in episodes:
                if episode.get("_transcript_vtt"):
                    from .assets import check_vtt
                    atomic_write(output / "transcripts" / (chapter_filename(episode['guid']) + ".vtt"),
                                 check_vtt(episode["_transcript_vtt"]).encode("utf-8"))
                if episode.get("chapters"):
                    chapters = {"version": "1.2.0", "chapters": episode["chapters"]}
                    atomic_write(output / "chapters" / chapter_filename(episode['guid']),
                                 (json.dumps(chapters, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode())
            atomic_write(output / "feed.xml", data)
            # A crash before this commit safely repeats replacement, not append.
            with self.db.connection() as conn:
                conn.executemany("UPDATE episodes SET status='published' WHERE show_id=? AND guid=?",
                                 [(show_id, r["guid"]) for r in selected])
                conn.execute("UPDATE shows SET dirty=0 WHERE id=?", (show_id,))
        return sum(r["status"] != "published" for r in selected)
