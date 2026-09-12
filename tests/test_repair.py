"""Applying a repair. Every test is time-boxed: this path once deadlocked.

repair_show holds db.lock() across the whole repair and calls helpers that take
it again, so a lock that is not re-entrant hangs here rather than failing.
"""

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


def test_repair_regenerates_the_feed(db, published):
    scan = scan_show(db, published["id"])
    backup, result = repair_show(db, scan, {}, None)
    assert Path(backup).is_file()
    assert (asset_root(published) / "feed.xml").is_file()
    assert result["issues"] == []


def test_repair_writes_a_usable_backup(db, published):
    scan = scan_show(db, published["id"])
    backup, _ = repair_show(db, scan, {}, None)
    with ZipFile(backup) as archive:
        assert "manifest.json" in archive.namelist()


def test_repair_recovers_a_missing_file_from_a_local_source(db, published, tmp_path):
    assets = asset_root(published)
    (assets / "audio" / "ep1.mp3").unlink()
    scan = scan_show(db, published["id"])
    missing = [m for m in scan["missing"] if m["path"].endswith("ep1.mp3")]
    assert missing, scan["issues"]

    source = tmp_path / "recovered.mp3"
    source.write_bytes(b"recovered audio")
    repair_show(db, scan, {missing[0]["path"]: str(source)}, None)
    assert (assets / "audio" / "ep1.mp3").read_bytes() == b"recovered audio"


def test_repair_refuses_an_existing_output_directory(db, published, tmp_path):
    occupied = tmp_path / "already-there"
    occupied.mkdir()
    scan = scan_show(db, published["id"])
    with pytest.raises(ValueError, match="must be new"):
        repair_show(db, scan, {}, str(occupied))


def test_scan_reports_a_missing_referenced_asset(db, published):
    (asset_root(published) / "audio" / "ep1.mp3").unlink()
    scan = scan_show(db, published["id"])
    assert any("missing" in issue for issue in scan["issues"])
    assert [m["url"] for m in scan["missing"]] == ["https://example.org/show/audio/ep1.mp3"]
