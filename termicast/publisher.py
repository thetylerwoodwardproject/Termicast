"""Recoverable publication: durable intent, prepared assets, feed written last.

Publication coordinates a per-show operation lock, the cross-show database
lock, and a per-show output lock. Network (S3) uploads run with only the
operation lock held so other shows can use the database meanwhile.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from .database import filesystem_lock
from .feed import render_feed
from .validation import validate_episode
from .models import ASSET_ROLES, chapters_relative, transcript_relative
from .storage import asset_root, asset_base, local_relative


def fsync_dir(path):
    """Fsync a directory so a preceding create/replace/link is durable."""
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


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
        fsync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def operation_lock(show):
    return filesystem_lock(asset_root(show) / ".termicast.oplock")


def output_lock(show):
    return filesystem_lock(asset_root(show) / ".termicast.lock")


def episode_asset_paths(episode, show=None):
    """Relative managed asset paths that must exist for this episode.

    Audio, artwork, and transcript paths come from their stored path field
    when present; imported episodes carry only a public URL, so the relative
    path is recovered from that URL via `local_relative` when `show` is
    supplied. Chapter images likewise carry no stored path and need `show`.
    Without `show`, only stored path fields and the recomputed chapter and
    transcript paths are returned.
    """
    paths = set()
    for field, path_field in ASSET_ROLES:
        relative = episode.get(path_field)
        if show is not None and not relative:
            relative = local_relative(show, episode.get(field))
        if relative:
            paths.add(str(relative))
    if episode.get("chapters"):
        paths.add(chapters_relative(episode))
        if show is not None:
            for chapter in episode["chapters"]:
                relative = local_relative(show, chapter.get("img"))
                if relative:
                    paths.add(str(relative))
    if episode.get("_transcript_vtt"):
        paths.add(transcript_relative(episode))
    return paths


def _verified_urls(show, relative_paths):
    """Public URLs that deploy_paths already verified for these uploads."""
    base = asset_base(show)
    return {f"{base}/{relative}" for relative in relative_paths}


def _verify_published_assets(show, episodes, skip=()):
    """Fail loudly if any published episode's assets aren't publicly reachable.

    A deploy that silently skips an asset type would otherwise report success;
    this checks the full published set, not just the files this call uploaded,
    so such a gap surfaces immediately. `skip` carries the URLs the upload
    already verified: covering them twice doubles the request count without
    widening the coverage this exists for.
    """
    from .hosting import check_assets, summarize_verification_problems
    problems = check_assets(show, episodes, skip=skip)
    if problems:
        raise RuntimeError("\n".join(summarize_verification_problems(problems, target="s3")))


def upload_existing_assets(show, relative_paths, dry_run=False, verify=True):
    """Upload local asset files that exist, then optionally remove them.

    Media already on S3 has no local copy when `keep_local_media` is off, so
    missing local files are skipped (the remote object is retained). Uploaded
    files are removed afterward unless `keep_local_media` is enabled.
    """
    from .s3deploy import deploy_paths
    root = asset_root(show)
    existing = [relative for relative in relative_paths if (root / relative).is_file()]
    if not existing:
        return []
    uploaded = deploy_paths(show, existing, dry_run=dry_run, verify=verify)
    if not show.get("keep_local_media") and not dry_run:
        for relative in uploaded:
            (root / relative).unlink(missing_ok=True)
    return uploaded


def mirror_feed(show, *, dry_run=False, verify=True):
    """Upload/verify the freshly written canonical feed.xml to S3.

    Unlike `upload_existing_assets`, this never deletes the local file: the
    feed is the canonical, always-local copy. It goes through `deploy_paths`
    so the mirror reuses ordered uploads, public MIME verification, and
    dry-run handling consistently with the rest of a deploy.
    """
    from .s3deploy import deploy_paths
    return deploy_paths(show, ["feed.xml"], dry_run=dry_run, verify=verify)


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
        due = when is None or datetime.fromisoformat(target) <= now
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
        if due:
            self._write(show_id, now, include_due=True)
        return episode["guid"]

    def regenerate(self, show_id):
        self._write(show_id, datetime.now(timezone.utc), include_due=False)

    def merge(self, show_id, episodes):
        """Preflight a complete batch under the writer lock, then persist intent.

        A failed file replacement leaves every row and the dirty flag committed;
        publish-due can recover without replaying a partially applied CSV.
        """
        now = datetime.now(timezone.utc)
        with self.db.lock():
            show = self.db.get_show(show_id)
            if show is None:
                raise ValueError("Unknown podcast ID")
            existing = {e["guid"]: e for e in self.db.list_episodes(show_id)}
            if callable(episodes):
                episodes = episodes(existing, show)
            prepared, seen, releases = [], set(), set()
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
            with self.db.connection() as conn:
                template = conn.execute("SELECT template FROM shows WHERE id=?", (show_id,)).fetchone()[0]
            preview = {guid: e for guid, e in existing.items() if e["status"] == "published"}
            for guid, _, data, _, status in prepared:
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
        with self.db.lock():
            with self.db.connection() as conn:
                ids = [row[0] for row in conn.execute("""
                    SELECT id FROM shows WHERE dirty=1 OR id IN
                    (SELECT show_id FROM episodes WHERE status='scheduled' AND publish_at<=?)
                    """, (now.isoformat(),))]
        count = 0
        failures = []
        for show_id in ids:
            try:
                count += self._write(show_id, now, include_due=True)
            except Exception as exc:
                failures.append(f"{show_id}: {exc}")
        if failures:
            raise RuntimeError(f"Published {count} episode(s); retry failed podcasts: " + "; ".join(failures))
        return count

    def deploy(self, show_id, dry_run=False, verify=True):
        """Standalone deploy: upload saved assets (S3) and verify.

        `feed.xml` stays on the web server as the canonical copy; when the
        show's `mirror_feed` setting is on, a copy is additionally uploaded to
        the bucket after the media assets succeed.
        """
        show = self.db.get_show(show_id)
        if show is None:
            raise ValueError("Unknown podcast ID")
        asset_root(show).mkdir(parents=True, exist_ok=True)
        with operation_lock(show):
            with self.db.lock():
                episodes = [e for e in self.db.list_episodes(show_id) if e["status"] == "published"]
            assets = set()
            artwork = local_relative(show, show.get("artwork_url"))
            if artwork:
                assets.add(str(artwork))
            for episode in episodes:
                assets |= episode_asset_paths(episode, show)
            if show.get("hosting") == "s3":
                if show.get("mirror_feed") and not (asset_root(show) / "feed.xml").is_file():
                    raise ValueError("No local feed.xml to deploy; publish or regenerate first")
                root = asset_root(show)
                if not dry_run and (any((root / relative).is_file() for relative in assets)
                                    or (show.get("mirror_feed") and (root / "feed.xml").is_file())):
                    from .s3deploy import check_s3_access
                    check_s3_access(show)
                uploaded = upload_existing_assets(show, sorted(assets), dry_run=dry_run, verify=verify)
                if show.get("mirror_feed"):
                    uploaded = list(uploaded) + list(mirror_feed(show, dry_run=dry_run, verify=verify))
                if verify and not dry_run:
                    _verify_published_assets(show, episodes, skip=_verified_urls(show, uploaded))
                return uploaded
            feed = asset_root(show) / "feed.xml"
            if not feed.is_file():
                raise ValueError("No local feed.xml to deploy; publish or regenerate first")
            if verify and not dry_run:
                from .hosting import check_url
                from .media import content_type_for
                problems = list(check_url(asset_base(show) + "/feed.xml", "application/rss+xml"))
                for relative in sorted(assets):
                    problems.extend(check_url(asset_base(show) + "/" + relative, content_type_for(relative)))
                if problems:
                    from .hosting import summarize_verification_problems
                    raise RuntimeError("\n".join(summarize_verification_problems(problems, target="local")))
            return sorted(assets | {"feed.xml"})

    def _snapshot(self, show_id, now, include_due, release_guids):
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM shows WHERE id=?", (show_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown podcast ID")
            show = json.loads(row["settings"])
            template = row["template"]
            rows = conn.execute("SELECT * FROM episodes WHERE show_id=? ORDER BY publish_at DESC", (show_id,)).fetchall()
        selected = [r for r in rows if r["status"] == "published" or
                    ((include_due or r["guid"] in release_guids) and r["publish_at"] <= now.isoformat())]
        episodes = [json.loads(r["data"]) for r in selected]
        selected_guids = {r["guid"] for r in selected}
        data = render_feed(show, template, episodes, now,
                           excluded_guids={r["guid"] for r in rows} - selected_guids)
        return {"show": show, "selected": selected, "episodes": episodes, "data": data}

    def _write(self, show_id, now, include_due, release_guids=()):
        with self.db.lock():
            show = self.db.get_show(show_id)
            if show is None:
                raise ValueError("Unknown podcast ID")
        asset_root(show).mkdir(parents=True, exist_ok=True)
        with operation_lock(show):
            with self.db.lock():
                snapshot = self._snapshot(show_id, now, include_due, release_guids)
                with self.db.connection() as conn:
                    conn.execute("UPDATE shows SET dirty=1 WHERE id=?", (show_id,))
            with output_lock(show):
                self._write_assets(snapshot)
            if show.get("hosting") == "s3" and show.get("enabled"):
                self._deploy_remote(snapshot, show)
            with output_lock(show):
                atomic_write(asset_root(show) / "feed.xml", snapshot["data"])
            if show.get("hosting") == "s3" and show.get("enabled") and show.get("mirror_feed"):
                try:
                    mirror_feed(show)
                except Exception as exc:
                    raise RuntimeError(
                        f"Local feed.xml and media are published, but mirroring feed.xml to S3 "
                        f"failed: {exc}. Retry with: termicast publish-due (it retries every "
                        f"dirty or due show).") from exc
            with self.db.lock():
                with self.db.connection() as conn:
                    conn.executemany("UPDATE episodes SET status='published' WHERE show_id=? AND guid=?",
                                     [(show_id, r["guid"]) for r in snapshot["selected"]])
                    conn.execute("UPDATE shows SET dirty=0 WHERE id=?", (show_id,))
        return sum(r["status"] != "published" for r in snapshot["selected"])

    def _write_assets(self, snapshot):
        show = snapshot["show"]
        assets = asset_root(show)
        assets.mkdir(parents=True, exist_ok=True)
        for episode in snapshot["episodes"]:
            if episode.get("_transcript_vtt"):
                from .assets import check_vtt
                atomic_write(assets / transcript_relative(episode),
                             check_vtt(episode["_transcript_vtt"]).encode("utf-8"))
            if episode.get("chapters"):
                chapters = {"version": "1.2.0", "chapters": episode["chapters"]}
                atomic_write(assets / chapters_relative(episode),
                             (json.dumps(chapters, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode())

    def _deploy_remote(self, snapshot, show):
        paths = set()
        artwork = local_relative(show, show.get("artwork_url"))
        if artwork:
            paths.add(str(artwork))
        for episode in snapshot["episodes"]:
            paths |= episode_asset_paths(episode, show)
        try:
            from .s3deploy import check_s3_access
            root = asset_root(show)
            if any((root / relative).is_file() for relative in paths):
                check_s3_access(show)
            uploaded = upload_existing_assets(show, sorted(paths))
            published = [dict(json.loads(row["data"]), status=row["status"])
                         for row in snapshot["selected"] if row["status"] == "published"]
            if published:
                _verify_published_assets(show, published, skip=_verified_urls(show, uploaded))
        except Exception as exc:
            raise RuntimeError(
                f"Saved locally. Remote publication failed: {exc}. "
                f"Retry with: termicast publish-due (it retries every dirty or due show, not "
                f"just this one); use termicast deploy {show['id']} for a standalone upload "
                f"retry after the feed has already been regenerated.") from exc
