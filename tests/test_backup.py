"""Backup archive naming and contents."""

import zipfile
from pathlib import Path

from termicast.backup import create_backup
from termicast.publisher import Publisher

from test_publisher import make_episode


def _write_media(show):
    for relative in ("audio/e1.mp3", "images/e1.jpg", "transcripts/e1.vtt"):
        path = Path(show["output_dir"]) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")


def test_backup_with_media_is_named_for_the_archive_not_the_last_file(db, show, tmp_path):
    """The ZIP keeps its termicast-<timestamp>-<id>.zip name.

    The include_media directory walk binds `name` per entry, which used to
    clobber the archive filename computed at the top of create_backup, so a
    media backup landed as e.g. "s01ep001.vtt" -- a ZIP wearing a transcript's
    name, which nothing would recognise as a backup.
    """
    Publisher(db).publish(show["id"], make_episode())
    _write_media(show)

    result = create_backup(db, tmp_path / "backups", include_media=True)

    assert result.name.startswith("termicast-"), result.name
    assert result.suffix == ".zip", result.name
    assert result.is_file()
    assert zipfile.is_zipfile(result)


def test_backup_with_media_includes_the_media(db, show, tmp_path):
    Publisher(db).publish(show["id"], make_episode())
    _write_media(show)

    result = create_backup(db, tmp_path / "backups", include_media=True)
    with zipfile.ZipFile(result) as archive:
        names = archive.namelist()

    assert "termicast.db" in names
    assert "manifest.json" in names
    assert any(n.endswith("audio/e1.mp3") for n in names), names
    assert any(n.endswith("feed.xml") for n in names), names
