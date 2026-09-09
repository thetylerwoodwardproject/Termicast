from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from threading import Event, get_ident
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest

from termicast import backup as backup_module
from termicast import database as database_module
from termicast.backup import create_backup
from termicast.database import Database, filesystem_lock
from termicast.importer import import_feed
from termicast.models import new_episode, new_show
from termicast.publisher import Publisher
from termicast.validation import validate_episode, validate_show


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "state" / "termicast.db")


@pytest.fixture
def show_factory(db, tmp_path):
    def create(name="show", imported=False):
        template = None
        if imported:
            show, template = import_feed(str(Path(__file__).parent / "fixtures" / "sample.xml"))
        else:
            show = new_show(
                title=name, description="A test podcast", author="Test author",
                owner_name="Test owner", owner_email="owner@example.org",
                website=f"https://example.org/{name}", category="Technology",
            )
        show.update(output_dir=str(tmp_path / name), base_url=f"https://example.org/{name}")
        assert validate_show(show) == []
        db.save_show(show, template)
        return show

    return create


@pytest.fixture
def show(show_factory):
    return show_factory()


@pytest.fixture
def episode():
    record = new_episode(
        title="An episode", description="<p>News & stories</p>",
        mp3_url="https://example.org/episode.mp3", length=123456, duration=120,
        chapters=[{"startTime": 0, "endTime": 120, "title": "Opening & discussion"}],
    )
    assert validate_episode(record) == []
    return record


def test_archive_preserves_saved_state_and_exact_outputs(db, show_factory, episode, tmp_path):
    shows = [show_factory("imported", imported=True), show_factory("native")]
    publisher = Publisher(db)
    published = datetime(2000, 1, 2, 3, 4, tzinfo=timezone(timedelta(hours=-5)))
    scheduled = datetime(9998, 6, 7, 8, 9, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    expected_files = {}
    for show in shows:
        publisher.publish(show["id"], episode | {"guid": str(uuid4())}, published)
        publisher.publish(show["id"], episode | {"guid": str(uuid4())}, scheduled)
        output = Path(show["output_dir"])
        # Imported/stale chapter files are included even without a database episode.
        (output / "chapters" / "legacy.json").write_bytes(b'{ "chapters": [] }\r\n')
        for path in [output / "feed.xml", *(output / "chapters").glob("*.json")]:
            expected_files[f"outputs/{show['id']}/{path.relative_to(output).as_posix()}"] = path.read_bytes()
        for name in ("episode.mp3", "cover.jpg", "notes.txt", "private.json", "chapters/notes.txt"):
            (output / name).write_bytes(b"not part of a backup")
        (output / "media").mkdir()
        (output / "media" / "episode.mp3").write_bytes(b"media")
    db.save_show(shows[0] | {"title": "Saved but not regenerated"})
    (db.path.parent / "unrelated.txt").write_bytes(b"private unrelated state")
    with db.connection() as conn:
        original_shows = [tuple(row) for row in conn.execute("SELECT * FROM shows ORDER BY id")]
        original_episodes = [tuple(row) for row in conn.execute("SELECT * FROM episodes ORDER BY guid")]
    source_bytes = db.path.read_bytes()
    before = datetime.now(timezone.utc)
    result = create_backup(db, tmp_path / "private")
    after = datetime.now(timezone.utc)
    with ZipFile(result) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == {"termicast.db", "manifest.json", *expected_files}
        assert len(archive.namelist()) == len(set(archive.namelist()))
        for name, data in expected_files.items():
            assert archive.read(name) == data
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["version"] == 1
        assert manifest["database"] == "termicast.db"
        assert manifest["source_database"] == str(db.path)
        created = datetime.fromisoformat(manifest["created_at"])
        assert created.utcoffset() == timedelta(0)
        assert before <= created <= after
        assert manifest["missing_files"] == []
        assert manifest["excludes"] == ["unsaved forms", "externally hosted assets", "local media"]
        assert manifest["shows"] == [
            {"id": s["id"], "title": db.get_show(s["id"])["title"],
             "output_dir": s["output_dir"], "archive_dir": f"outputs/{s['id']}"}
            for s in shows
        ]
        snapshot = tmp_path / "snapshot.db"
        snapshot.write_bytes(archive.read("termicast.db"))
    with sqlite3.connect(snapshot) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT * FROM shows ORDER BY id").fetchall() == original_shows
        assert conn.execute("SELECT * FROM episodes ORDER BY guid").fetchall() == original_episodes
        assert dict(conn.execute("SELECT id, dirty FROM shows")) == {shows[0]["id"]: 1, shows[1]["id"]: 0}
        template = conn.execute("SELECT template FROM shows WHERE id=?", (shows[0]["id"],)).fetchone()[0]
        assert template == (Path(__file__).parent / "fixtures" / "sample.xml").read_bytes()
        assert json.loads(conn.execute("SELECT settings FROM shows WHERE id=?", (shows[0]["id"],)).fetchone()[0])["guid"] == shows[0]["guid"]
        for data, target, status in conn.execute("SELECT data, publish_at, status FROM episodes"):
            expected = published if status == "published" else scheduled
            assert target == json.loads(data)["published_at"] == expected.astimezone(timezone.utc).isoformat()
        assert dict(conn.execute("SELECT status, count(*) FROM episodes GROUP BY status")) == {"published": 2, "scheduled": 2}
    assert db.path.read_bytes() == source_bytes
    for show in shows:
        output = Path(show["output_dir"])
        for path in [output / "feed.xml", *(output / "chapters").glob("*.json")]:
            assert path.read_bytes() == expected_files[f"outputs/{show['id']}/{path.relative_to(output).as_posix()}"]


def test_missing_outputs_are_recorded_not_created(db, show_factory, episode, tmp_path):
    missing_show = show_factory("never-published")
    show = show_factory("published")
    Publisher(db).publish(show["id"], episode)
    output = Path(show["output_dir"])
    chapter = output / "chapters" / f"{episode['guid']}.json"
    chapter.unlink()
    (output / "feed.xml").unlink()
    Publisher(db).publish(show["id"], episode | {"guid": str(uuid4())}, datetime(9998, 1, 1, tzinfo=timezone.utc))
    with ZipFile(create_backup(db, tmp_path / "private")) as archive:
        assert set(archive.namelist()) == {"termicast.db", "manifest.json"}
        manifest = json.loads(archive.read("manifest.json"))
        assert set(manifest["missing_files"]) == {
            str(Path(missing_show["output_dir"]) / "feed.xml"), str(output / "feed.xml"), str(chapter),
        }
        assert len(manifest["shows"]) == 2
    assert not Path(missing_show["output_dir"]).exists()
    assert not chapter.exists()
    assert not (output / "feed.xml").exists()


def test_empty_database_default_destination_and_private_permissions(db, tmp_path):
    result = create_backup(db)
    assert result.parent == db.path.parent / "backups"
    assert result.name.startswith("termicast-") and result.suffix == ".zip"
    assert result.stat().st_mode & 0o777 == 0o600
    assert result.parent.stat().st_mode & 0o777 == 0o700
    assert list(result.parent.iterdir()) == [result]
    with ZipFile(result) as archive:
        assert set(archive.namelist()) == {"termicast.db", "manifest.json"}
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["shows"] == manifest["missing_files"] == []
        snapshot = tmp_path / "empty.db"
        snapshot.write_bytes(archive.read("termicast.db"))
    with sqlite3.connect(snapshot) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("SELECT * FROM shows").fetchall() == []
        assert conn.execute("SELECT * FROM episodes").fetchall() == []


@pytest.mark.parametrize("kind", ["exact", "child", "symlink"])
def test_rejects_public_destination(db, show_factory, tmp_path, kind):
    show_factory("first")
    show = show_factory("second")
    output = Path(show["output_dir"])
    output.mkdir()
    destination = output if kind == "exact" else output / "backups"
    if kind == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(output, target_is_directory=True)
        destination = alias / "backups"
    original = db.path.read_bytes()
    with pytest.raises(ValueError, match="outside every podcast"):
        create_backup(db, destination)
    assert list(output.iterdir()) == []
    assert db.path.read_bytes() == original


@pytest.mark.parametrize("kind", ["feed", "chapters", "chapter", "dangling-feed", "output"])
def test_rejects_symbolic_link_outputs(db, show, episode, tmp_path, kind):
    Publisher(db).publish(show["id"], episode)
    output = Path(show["output_dir"])
    path = {"feed": output / "feed.xml", "dangling-feed": output / "feed.xml",
            "chapters": output / "chapters", "chapter": output / "chapters" / f"{episode['guid']}.json",
            "output": output}[kind]
    target = tmp_path / "original-output"
    path.rename(target)
    path.symlink_to(tmp_path / "absent" if kind == "dangling-feed" else target,
                    target_is_directory=target.is_dir())
    original = db.path.read_bytes()
    target_bytes = {p: p.read_bytes() for p in target.rglob("*") if p.is_file()} if target.is_dir() else {target: target.read_bytes()}
    destination = tmp_path / "private"
    with pytest.raises(ValueError, match="symbolic-link"):
        create_backup(db, destination)
    assert not destination.exists() or list(destination.iterdir()) == []
    assert path.is_symlink()
    assert db.path.read_bytes() == original
    assert all(p.read_bytes() == data for p, data in target_bytes.items())


def test_failed_archive_cleans_staging_and_preserves_originals(db, show, episode, tmp_path, monkeypatch):
    Publisher(db).publish(show["id"], episode)
    destination = tmp_path / "private"
    existing = create_backup(db, destination)
    originals = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    write = ZipFile.write
    calls = []

    def fail_write(archive, filename, arcname=None, *args, **kwargs):
        calls.append(arcname)
        write(archive, filename, arcname, *args, **kwargs)
        if arcname.endswith("feed.xml"):
            raise OSError("injected archive failure")

    monkeypatch.setattr(ZipFile, "write", fail_write)
    with pytest.raises(OSError, match="injected archive failure"):
        create_backup(db, destination)
    assert calls == ["termicast.db", f"outputs/{show['id']}/feed.xml"]
    assert list(destination.iterdir()) == [existing]
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == originals


def test_existing_backups_are_never_overwritten(db, tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 8, 12, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(backup_module, "datetime", Clock)
    monkeypatch.setattr(backup_module, "uuid4", lambda: UUID("87654321-0000-0000-0000-000000000000"))
    destination = tmp_path / "private"
    first = create_backup(db, destination)
    original = first.read_bytes()
    monkeypatch.setattr(backup_module, "uuid4", lambda: UUID("12345678-0000-0000-0000-000000000000"))
    second = create_backup(db, destination)
    assert first != second
    second_bytes = second.read_bytes()
    with pytest.raises(FileExistsError):
        create_backup(db, destination)
    assert first.read_bytes() == original
    assert second.read_bytes() == second_bytes
    assert set(destination.iterdir()) == {first, second}


@pytest.mark.parametrize("lock_kind", ["database", "publisher-output"])
def test_backup_waits_for_shared_lock(db, show, episode, tmp_path, monkeypatch, lock_kind):
    Publisher(db).publish(show["id"], episode)
    output = Path(show["output_dir"])
    lock_path = str(db.path) + ".lock" if lock_kind == "database" else output / ".termicast.lock"
    destination = tmp_path / "private"
    attempting, acquired = Event(), Event()
    main_thread = get_ident()
    flock = database_module.fcntl.flock
    count = 0

    def observed_flock(fd, operation):
        nonlocal count
        watched = False
        if get_ident() != main_thread and operation == database_module.fcntl.LOCK_EX:
            count += 1
            watched = count == (1 if lock_kind == "database" else 2)
        if watched:
            attempting.set()
        result = flock(fd, operation)
        if watched:
            acquired.set()
        return result

    monkeypatch.setattr(database_module.fcntl, "flock", observed_flock)
    # Release the held lock before joining the worker, including on assertion failure.
    with ThreadPoolExecutor(max_workers=1) as executor:
        with filesystem_lock(lock_path):
            future = executor.submit(create_backup, db, destination)
            assert attempting.wait(5)
            assert not acquired.wait(0.1)
            assert not future.done()
            assert not destination.exists()
        result = future.result(timeout=5)
    assert acquired.is_set()
    with ZipFile(result) as archive:
        assert archive.testzip() is None
        assert archive.read(f"outputs/{show['id']}/feed.xml") == (output / "feed.xml").read_bytes()
