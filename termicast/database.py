"""SQLite state and a cross-process lock shared by menus and cron."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3

from .validation import validate_show


@contextmanager
def filesystem_lock(path):
    with open(path, "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Database:
    def __init__(self, path=None):
        home = Path(os.environ.get("TERMICAST_HOME", Path.home() / ".local/share/termicast"))
        self.path = Path(path).expanduser().resolve() if path else (home.expanduser() / "termicast.db").resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock(), self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS shows (
                    id TEXT PRIMARY KEY, settings TEXT NOT NULL,
                    output_dir TEXT NOT NULL UNIQUE, template BLOB,
                    dirty INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS episodes (
                    guid TEXT NOT NULL,
                    show_id TEXT NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
                    data TEXT NOT NULL, publish_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('scheduled', 'published')),
                    PRIMARY KEY(show_id, guid)
                );
                CREATE INDEX IF NOT EXISTS due_episodes ON episodes(status, publish_at);
            """)
            keys = {row["name"]: row["pk"] for row in conn.execute("PRAGMA table_info(episodes)")}
            if not keys.get("show_id"):
                conn.executescript("""
                    BEGIN IMMEDIATE;
                    ALTER TABLE episodes RENAME TO old_episodes;
                    CREATE TABLE episodes (
                        guid TEXT NOT NULL, show_id TEXT NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
                        data TEXT NOT NULL, publish_at TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('scheduled', 'published')),
                        PRIMARY KEY(show_id, guid));
                    INSERT INTO episodes SELECT * FROM old_episodes;
                    DROP TABLE old_episodes;
                    CREATE INDEX due_episodes ON episodes(status, publish_at);
                    COMMIT;
                """)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def lock(self):
        return filesystem_lock(str(self.path) + ".lock")

    def list_shows(self):
        with self.connection() as conn:
            return [json.loads(row[0]) for row in conn.execute("SELECT settings FROM shows ORDER BY rowid")]

    def get_show(self, show_id):
        with self.connection() as conn:
            row = conn.execute("SELECT settings FROM shows WHERE id = ?", (show_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def save_show(self, show, template=None, episodes=None):
        show = dict(show)
        errors = validate_show(show)
        if errors:
            raise ValueError("; ".join(errors))
        show["output_dir"] = str(Path(show["output_dir"]).expanduser().resolve())
        with self.lock(), self.connection() as conn:
            previous = conn.execute("SELECT settings FROM shows WHERE id = ?", (show["id"],)).fetchone()
            if previous and json.loads(previous[0])["guid"] != show["guid"]:
                raise ValueError("A podcast GUID cannot change after creation")
            conn.execute("""
                INSERT INTO shows(id, settings, output_dir, template) VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET settings=excluded.settings,
                    output_dir=excluded.output_dir, template=COALESCE(excluded.template, shows.template), dirty=1
                """, (show["id"], json.dumps(show), show["output_dir"], template))
            for episode in episodes or []:
                conn.execute("INSERT INTO episodes VALUES (?, ?, ?, ?, 'published')",
                             (episode["guid"], show["id"], json.dumps(episode), episode["published_at"]))

    def forget_show(self, show_id):
        with self.lock(), self.connection() as conn:
            conn.execute("DELETE FROM shows WHERE id = ?", (show_id,))

    def list_episodes(self, show_id):
        from .importer import extract_episodes
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM episodes WHERE show_id = ? ORDER BY publish_at DESC", (show_id,))
            episodes = [dict(json.loads(row["data"]), status=row["status"], publish_at=row["publish_at"]) for row in rows]
            show = conn.execute("SELECT template, settings FROM shows WHERE id=?", (show_id,)).fetchone()
            known = {e["guid"] for e in episodes}
            if show and show["template"]:
                for episode in extract_episodes(show["template"], json.loads(show["settings"]).get("import_url_map")):
                    if episode["guid"] not in known:
                        episodes.append(dict(episode, status="published", publish_at=episode["published_at"]))
            return sorted(episodes, key=lambda e: e["publish_at"], reverse=True)
