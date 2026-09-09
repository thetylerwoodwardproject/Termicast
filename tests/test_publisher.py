from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import multiprocessing
from pathlib import Path
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

from lxml import etree
import pytest

from termicast.database import Database, filesystem_lock
from termicast.feed import NS, validate_feed
from termicast.models import new_episode, new_show
from termicast import publisher as publisher_module
from termicast.publisher import Publisher, atomic_write
from termicast.validation import validate_episode, validate_show


@pytest.fixture
def clock(monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz)

    monkeypatch.setattr(publisher_module, "datetime", Clock)
    return Clock


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "state" / "termicast.db")


@pytest.fixture
def show_factory(db, tmp_path):
    def create(name="show"):
        show = new_show(
            title=name, description="A test podcast", author="Test author",
            owner_name="Test owner", owner_email="owner@example.org",
            website=f"https://example.org/{name}", category="Technology",
            base_url=f"https://example.org/{name}",
            output_dir=str(tmp_path / name), timezone="America/New_York",
        )
        assert validate_show(show) == []
        db.save_show(show)
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
        chapters=[{"startTime": 0, "endTime": 30.5, "title": "Opening & intro"},
                  {"startTime": 30.5, "endTime": 120, "title": "Discussion"}],
    )
    assert validate_episode(record) == []
    return record


def read_feed(show):
    data = (Path(show["output_dir"]) / "feed.xml").read_bytes()
    assert validate_feed(data) == []
    return etree.fromstring(data)


def item_guids(show):
    return read_feed(show).xpath("channel/item/guid/text()")


def chapter_path(show, episode):
    return Path(show["output_dir"]) / "chapters" / f"{episode['guid']}.json"


def test_immediate_prepends_and_writes_chapters_and_utc(db, show, episode, clock):
    publisher = Publisher(db)
    older = episode | {"guid": str(uuid4()), "title": "Earlier"}
    publisher.publish(show["id"], older, clock.current - timedelta(days=1))
    assert publisher.publish(show["id"], episode) == episode["guid"]
    assert item_guids(show) == [episode["guid"], older["guid"]]
    assert json.loads(chapter_path(show, episode).read_text()) == {
        "version": "1.2.0", "chapters": episode["chapters"],
    }
    item = read_feed(show).find("channel/item")
    assert item.find("podcast:chapters", NS).attrib == {
        "url": f"{show['base_url']}/chapters/{episode['guid']}.json",
        "type": "application/json+chapters",
    }
    assert len(item.findall("psc:chapters/psc:chapter", NS)) == 2
    assert parsedate_to_datetime(item.findtext("pubDate")) == clock.current
    assert parsedate_to_datetime(read_feed(show).findtext("channel/lastBuildDate")) == clock.current
    saved = db.list_episodes(show["id"])[0]
    assert saved["status"] == "published"
    assert saved["publish_at"] == saved["published_at"] == clock.current.isoformat()
    assert "published_at" not in episode


@pytest.mark.parametrize("fold", [0, 1])
def test_schedule_stays_private_until_exact_utc_boundary(db, show, episode, clock, fold):
    publisher = Publisher(db)
    target = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo(show["timezone"]), fold=fold)
    utc = target.astimezone(timezone.utc)
    publisher.publish(show["id"], episode, target)
    assert not (Path(show["output_dir"]) / "feed.xml").exists()
    assert not chapter_path(show, episode).exists()
    saved = db.list_episodes(show["id"])[0]
    assert saved["status"] == "scheduled"
    assert saved["publish_at"] == saved["published_at"] == utc.isoformat()
    with db.connection() as conn:
        row = conn.execute("SELECT data, publish_at FROM episodes").fetchone()
        assert json.loads(row["data"])["published_at"] == row["publish_at"] == utc.isoformat()
    publisher.regenerate(show["id"])
    assert item_guids(show) == []
    clock.current = utc - timedelta(microseconds=1)
    assert publisher.publish_due() == 0
    assert item_guids(show) == []
    assert not chapter_path(show, episode).exists()
    clock.current = utc
    assert publisher.publish_due() == 1
    assert item_guids(show) == [episode["guid"]]
    assert chapter_path(show, episode).is_file()
    assert parsedate_to_datetime(read_feed(show).findtext("channel/item/pubDate")) == utc
    assert publisher.publish_due() == 0


def test_due_processes_multiple_shows_and_leaves_future_private(db, show_factory, episode, clock):
    publisher = Publisher(db)
    shows = [show_factory("first"), show_factory("second")]
    due = []
    for show in shows:
        records = [episode | {"guid": str(uuid4())} for _ in range(3)]
        for index, record in enumerate(records, 1):
            publisher.publish(show["id"], record, clock.current + timedelta(hours=index))
        due.append(records)
    clock.current += timedelta(hours=2)
    assert publisher.publish_due() == 4
    for show, records in zip(shows, due):
        assert item_guids(show) == [records[1]["guid"], records[0]["guid"]]
        assert not chapter_path(show, records[2]).exists()
        assert [e["status"] for e in db.list_episodes(show["id"])] == ["scheduled", "published", "published"]
        assert parsedate_to_datetime(read_feed(show).findtext("channel/lastBuildDate")) == clock.current
    assert publisher.publish_due() == 0


def test_duplicate_retry_preserves_original_timestamp(db, show, episode, clock):
    publisher = Publisher(db)
    publisher.publish(show["id"], episode)
    original = db.list_episodes(show["id"])
    clock.current += timedelta(hours=1)
    publisher.publish(show["id"], episode)
    publisher.regenerate(show["id"])
    assert publisher.publish_due() == 0
    assert db.list_episodes(show["id"]) == original
    assert item_guids(show) == [episode["guid"]]


@pytest.mark.parametrize("conflict", ["content", "schedule"])
def test_conflicting_duplicate_rejected(db, show, show_factory, episode, clock, conflict):
    publisher = Publisher(db)
    target = clock.current + timedelta(days=1)
    publisher.publish(show["id"], episode, target)
    publisher.publish(show["id"], episode, target)
    original = db.list_episodes(show["id"])
    other = show_factory("other")
    with pytest.raises(ValueError, match="GUID already exists"):
        publisher.publish(
            other["id"] if conflict == "show" else show["id"],
            episode | {"title": "Changed"} if conflict == "content" else episode,
            target + timedelta(seconds=1) if conflict == "schedule" else target,
        )
    assert db.list_episodes(show["id"]) == original
    assert db.list_episodes(other["id"]) == []


def test_invalid_publication_has_no_side_effects(db, show, episode):
    publisher = Publisher(db)
    with pytest.raises(ValueError, match="timezone"):
        publisher.publish(show["id"], episode, datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="Unknown podcast"):
        publisher.publish(str(uuid4()), episode)
    with pytest.raises(ValueError, match="title"):
        publisher.publish(show["id"], episode | {"title": "x" * 61})
    assert db.list_episodes(show["id"]) == []
    assert not Path(show["output_dir"]).exists()


@pytest.mark.parametrize("filename", ["feed.xml", "chapters/episode.json"])
@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_atomic_write_failure_preserves_old_file_and_cleans_temp(tmp_path, monkeypatch, filename, failure):
    path = tmp_path / filename
    old = b'<rss version="2.0"><channel/></rss>' if filename.endswith("xml") else b'{"chapters": []}'
    new = old + b"\n"
    atomic_write(path, old)
    replace = publisher_module.os.replace

    def fail(*args):
        raise OSError("injected atomic failure")

    def inspect_replace(source, destination):
        assert Path(source).parent == path.parent
        assert Path(source).read_bytes() == new
        assert path.read_bytes() == old
        return replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(publisher_module.os, failure, fail)
        with pytest.raises(OSError, match="injected atomic failure"):
            atomic_write(path, new)
    assert path.read_bytes() == old
    assert list(path.parent.iterdir()) == [path]
    monkeypatch.setattr(publisher_module.os, "replace", inspect_replace)
    atomic_write(path, new)
    assert path.read_bytes() == new
    assert path.stat().st_mode & 0o777 == 0o644
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("stage", ["chapter", "feed", "commit"])
def test_failed_publication_recovers_from_durable_intent(db, show, episode, clock, monkeypatch, stage):
    publisher = Publisher(db)
    publisher.regenerate(show["id"])
    feed_path = Path(show["output_dir"]) / "feed.xml"
    original = feed_path.read_bytes()
    write = publisher_module.atomic_write

    def fail_write(path, data):
        if (stage == "chapter" and Path(path).suffix == ".json") or (stage == "feed" and Path(path).name == "feed.xml"):
            raise OSError("injected write failure")
        write(path, data)

    if stage == "commit":
        # Abort after the episode status UPDATE, rolling back the whole transaction.
        with db.connection() as conn:
            conn.execute("""CREATE TRIGGER fail_commit BEFORE UPDATE OF dirty ON shows
                            BEGIN SELECT RAISE(ABORT, 'injected commit failure'); END""")
    with monkeypatch.context() as patch:
        patch.setattr(publisher_module, "atomic_write", fail_write)
        with pytest.raises((OSError, sqlite3.IntegrityError), match="injected"):
            publisher.publish(show["id"], episode)
    assert db.list_episodes(show["id"])[0]["status"] == "scheduled"
    if stage == "commit":
        assert item_guids(show) == [episode["guid"]]
        with db.connection() as conn:
            conn.execute("DROP TRIGGER fail_commit")
    else:
        assert feed_path.read_bytes() == original
    assert chapter_path(show, episode).exists() == (stage != "chapter")
    recovered = Publisher(Database(db.path))
    assert recovered.publish_due() == 1
    assert recovered.publish_due() == 0
    recovered.publish(show["id"], episode)
    assert item_guids(show) == [episode["guid"]]
    assert db.list_episodes(show["id"])[0]["status"] == "published"
    assert json.loads(chapter_path(show, episode).read_text())["chapters"] == episode["chapters"]


def test_due_continues_after_one_show_fails(db, show_factory, episode, clock, monkeypatch):
    shows = [show_factory("broken"), show_factory("healthy")]
    publisher = Publisher(db)
    for show in shows:
        publisher.publish(show["id"], episode | {"guid": str(uuid4())}, clock.current + timedelta(hours=1))
    clock.current += timedelta(hours=1)
    write = publisher_module.atomic_write

    def fail_broken(path, data):
        if Path(shows[0]["output_dir"]) in Path(path).parents:
            raise OSError("unwritable output")
        write(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(publisher_module, "atomic_write", fail_broken)
        with pytest.raises(RuntimeError, match=f"Published 1 episode.*{shows[0]['id']}.*unwritable output"):
            publisher.publish_due()
    assert db.list_episodes(shows[0]["id"])[0]["status"] == "scheduled"
    assert db.list_episodes(shows[1]["id"])[0]["status"] == "published"
    assert len(item_guids(shows[1])) == 1
    assert publisher.publish_due() == 1
    assert publisher.publish_due() == 0


def _publish_worker(db_path, show_id, episode, ready, start, finished):
    ready.set()
    if not start.wait(10):
        raise TimeoutError("Worker was not released")
    publisher = Publisher(Database(db_path))
    if episode is None:
        publisher.publish_due()
    else:
        publisher.publish(show_id, episode)
    finished.set()


@pytest.mark.parametrize("mode", ["distinct", "duplicate", "due"])
def test_concurrent_processes_do_not_duplicate_or_lose_episodes(db, show, episode, clock, mode):
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    records = [episode, episode if mode == "duplicate" else episode | {"guid": str(uuid4())}]
    if mode == "due":
        clock.current = datetime(2000, 1, 1, tzinfo=timezone.utc)
        for record in records:
            Publisher(db).publish(show["id"], record, clock.current + timedelta(days=1))
    work = [None, None] if mode == "due" else records
    ready = [context.Event() for _ in records]
    finished = [context.Event() for _ in records]
    processes = [context.Process(target=_publish_worker, args=(db.path, show["id"], record, signal, start, done))
                 for record, signal, done in zip(work, ready, finished)]
    try:
        for process in processes:
            process.start()
        assert all(signal.wait(10) for signal in ready)
        start.set()
        for process in processes:
            process.join(15)
            assert process.exitcode == 0
        expected = {record["guid"] for record in records}
        assert set(item_guids(show)) == expected
        assert len(item_guids(show)) == len(expected)
        assert len(db.list_episodes(show["id"])) == len(expected)
        assert all(record["status"] == "published" for record in db.list_episodes(show["id"]))
        assert all(chapter_path(show, record).is_file() for record in records)
        assert Publisher(db).publish_due() == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(5)


@pytest.mark.parametrize("lock_kind", ["database", "output"])
def test_publication_waits_for_filesystem_lock(db, show, episode, lock_kind):
    Publisher(db).regenerate(show["id"])
    context = multiprocessing.get_context("spawn")
    ready, start, finished = (context.Event() for _ in range(3))
    process = context.Process(target=_publish_worker, args=(db.path, show["id"], episode, ready, start, finished))
    lock_path = str(db.path) + ".lock" if lock_kind == "database" else Path(show["output_dir"]) / ".termicast.lock"
    try:
        with filesystem_lock(lock_path):
            process.start()
            assert ready.wait(10)
            start.set()
            assert not finished.wait(0.3)
            assert item_guids(show) == []
            assert not chapter_path(show, episode).exists()
        process.join(15)
        assert process.exitcode == 0
        assert finished.is_set()
        assert item_guids(show) == [episode["guid"]]
    finally:
        if process.is_alive():
            process.terminate()
        if process.pid is not None:
            process.join(5)


def test_forgetting_cascades_state_but_preserves_public_files(db, show, show_factory, episode, clock):
    other = show_factory("other")
    publisher = Publisher(db)
    publisher.publish(show["id"], episode)
    publisher.publish(show["id"], episode | {"guid": str(uuid4())}, clock.current + timedelta(days=1))
    output = Path(show["output_dir"])
    before = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    db.forget_show(show["id"])
    assert db.get_show(show["id"]) is None
    assert db.list_episodes(show["id"]) == []
    assert db.list_shows() == [other]
    assert Publisher(Database(db.path)).publish_due() == 0
    assert {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("alias", ["exact", "dotdot", "symlink"])
def test_output_directories_are_unique_after_resolution(db, show, show_factory, tmp_path, alias):
    other = show_factory("other")
    output = Path(show["output_dir"])
    if alias == "dotdot":
        output = output / ".." / output.name
    elif alias == "symlink":
        Path(show["output_dir"]).mkdir()
        output = tmp_path / "alias"
        output.symlink_to(show["output_dir"], target_is_directory=True)
    with pytest.raises(sqlite3.IntegrityError):
        db.save_show(other | {"output_dir": str(output)})
    assert db.get_show(other["id"]) == other
    with pytest.raises(sqlite3.IntegrityError):
        db.save_show(other | {"id": str(uuid4()), "output_dir": str(output)})
    assert len(db.list_shows()) == 2


def test_show_guid_is_immutable_when_settings_and_url_change(db, show):
    changed = show | {"title": "Renamed", "base_url": "https://new.example.org/podcast"}
    db.save_show(changed)
    assert Database(db.path).get_show(show["id"])["guid"] == show["guid"]
    with pytest.raises(ValueError, match="GUID cannot change"):
        db.save_show(changed | {"guid": str(uuid4())})
    assert db.get_show(show["id"]) == changed
    Publisher(db).regenerate(show["id"])
    root = read_feed(show)
    assert root.findtext("channel/podcast:guid", namespaces=NS) == show["guid"]
    assert root.find("channel/atom:link", NS).get("href") == changed["base_url"] + "/feed.xml"
