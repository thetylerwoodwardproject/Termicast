"""Applying a repair. Every test is time-boxed: this path once deadlocked.

repair_show holds db.lock() across the whole repair and calls helpers that take
it again, so a lock that is not re-entrant hangs here rather than failing.
"""

import json
import signal
from pathlib import Path
from zipfile import ZipFile

import pytest

from termicast.models import new_episode
from termicast.publisher import Publisher
from termicast.repair import repair_show, scan_show
from termicast.storage import asset_root


@pytest.fixture(autouse=True)
def time_box():
    """Fail rather than wedge the suite if the deadlock ever returns."""
    def bail(*args):
        raise TimeoutError("repair deadlocked: a lock was acquired twice")

    previous = signal.signal(signal.SIGALRM, bail)
    signal.alarm(15)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


@pytest.fixture
def published(db, show):
    """A show with one published episode and a generated feed."""
    assets = asset_root(show)
    (assets / "audio").mkdir(parents=True, exist_ok=True)
    (assets / "audio" / "ep1.mp3").write_bytes(b"audio")
    record = new_episode(
        guid="ep-1", title="Episode one", description="First", slug="ep1",
        mp3_url="https://example.org/show/audio/ep1.mp3", length=5, duration=60.0,
        audio_path="audio/ep1.mp3",
    )
    Publisher(db).publish(show["id"], record)
    return db.get_show(show["id"])


def test_repair_with_nothing_to_fix_returns(db, published):
    scan = scan_show(db, published["id"])
    backup, result = repair_show(db, scan, [], {}, None)
    assert Path(backup).is_file()
    assert (asset_root(published) / "feed.xml").is_file()
    assert result["fixes"] == []


def test_repair_writes_a_usable_backup(db, published):
    scan = scan_show(db, published["id"])
    backup, _ = repair_show(db, scan, [], {}, None)
    with ZipFile(backup) as archive:
        assert "manifest.json" in archive.namelist()


def test_repair_applies_only_the_selected_title_fix(db, show):
    # Publisher.publish rejects overlong titles, so these rows are written
    # directly: in practice they arrive from an import or an older database.
    publisher = Publisher(db)
    for guid, title in (("long-1", "A" * 80), ("long-2", "B" * 80)):
        record = new_episode(
            guid=guid, title="placeholder", description="d", slug=guid,
            mp3_url=f"https://example.org/show/audio/{guid}.mp3", length=5, duration=60.0)
        publisher.publish(show["id"], record)
        stored = next(e for e in db.list_episodes(show["id"]) if e["guid"] == guid)
        stored["title"] = title
        del stored["status"], stored["publish_at"]
        with db.connection() as conn:
            conn.execute("UPDATE episodes SET data=? WHERE show_id=? AND guid=?",
                         (json.dumps(stored), show["id"], guid))
    scan = scan_show(db, show["id"])
    assert {fix["guid"] for fix in scan["fixes"]} == {"long-1", "long-2"}

    repair_show(db, scan, ["long-1"], {}, None)
    titles = {e["guid"]: e["title"] for e in db.list_episodes(show["id"])}
    assert titles["long-1"] == "A" * 60
    assert titles["long-2"] == "B" * 80


def test_repair_recovers_a_missing_file_from_a_local_source(db, published, tmp_path):
    assets = asset_root(published)
    (assets / "audio" / "ep1.mp3").unlink()
    scan = scan_show(db, published["id"])
    missing = [m for m in scan["missing"] if m["path"].endswith("ep1.mp3")]
    assert missing, scan["issues"]

    source = tmp_path / "recovered.mp3"
    source.write_bytes(b"recovered audio")
    repair_show(db, scan, [], {missing[0]["path"]: str(source)}, None)
    assert (assets / "audio" / "ep1.mp3").read_bytes() == b"recovered audio"


def test_repair_refuses_an_existing_output_directory(db, published, tmp_path):
    occupied = tmp_path / "already-there"
    occupied.mkdir()
    scan = scan_show(db, published["id"])
    with pytest.raises(ValueError, match="must be new"):
        repair_show(db, scan, [], {}, str(occupied))


def test_repair_refuses_an_unknown_fix_selection(db, published):
    scan = scan_show(db, published["id"])
    with pytest.raises(ValueError, match="Unknown repair selection"):
        repair_show(db, scan, ["not-an-episode"], {}, None)


def test_scan_reports_a_missing_referenced_asset(db, published):
    (asset_root(published) / "audio" / "ep1.mp3").unlink()
    scan = scan_show(db, published["id"])
    assert any("missing" in issue for issue in scan["issues"])
    assert [m["url"] for m in scan["missing"]] == ["https://example.org/show/audio/ep1.mp3"]
