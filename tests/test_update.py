import csv
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock
from zipfile import ZipFile

from lxml import etree
from PIL import Image
import pytest

from termicast import cli, prompts, validation
from termicast.backup import create_backup
from termicast.csvio import FIELDS, export_csv, import_csv
from termicast.database import Database
from termicast.feed import NS
from termicast.importer import download_import, import_feed
from termicast.models import chapter_filename, new_episode, new_show
from termicast.publisher import Publisher


@pytest.fixture
def setup(tmp_path):
    db = Database(tmp_path / "state" / "termicast.db")
    show = new_show(title="Show", description="Description", base_url="https://new.example/show",
                    output_dir=str(tmp_path / "public"))
    db.save_show(show)
    return db, show, Publisher(db)


def episode(**kwargs):
    return new_episode(title="Episode", description="Description", length=12, duration=120,
                       mp3_url="https://example.org/audio.mp3", **kwargs)


def write_csv(path, rows, fields=FIELDS):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


@pytest.mark.parametrize("answers", [["x"], ["3", "Show", "Description", "Author", "Owner", "", "", "", "", "x"]])
def test_global_exit(tmp_path, monkeypatch, answers):
    monkeypatch.setenv("TERMICAST_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(prompts.console, "input", Mock(side_effect=answers))
    assert cli.main([]) == 0
    assert Database().list_shows() == []
    assert prompts._backup_action.get() is None


def test_x_text_is_literal(monkeypatch):
    monkeypatch.setattr(prompts.console, "input", lambda *a: "X")
    assert prompts.text("Title") == "X"


def test_opaque_ids_are_scoped_and_path_safe(setup, tmp_path):
    db, show, publisher = setup
    other = new_show(title="Other", description="Other", base_url="https://example.org/other",
                     output_dir=str(tmp_path / "other"))
    db.save_show(other)
    item = episode(guid="../../escaped/ID ?", chapters=[dict(startTime=0, endTime=120, title="Opening")])
    for record in (show, other):
        publisher.publish(record["id"], item)
        path = Path(record["output_dir"]) / "chapters" / chapter_filename(item["guid"])
        assert path.is_file()
        root = etree.parse(str(Path(record["output_dir"]) / "feed.xml"))
        assert root.findtext("channel/item/guid") == item["guid"]
        assert root.find("channel/item/podcast:chapters", NS).get("url").endswith(path.name)
    assert len(db.list_episodes(show["id"])) == len(db.list_episodes(other["id"])) == 1


def test_csv_roundtrip_edit_and_filters(setup, tmp_path):
    db, show, publisher = setup
    published = episode(guid="original-id", chapters=[dict(startTime=0, endTime=120, title="Chapter",
                                                          img="https://example.org/chapter.png")],
                        soundbites=[dict(startTime=10, duration=20, title="Clip")])
    publisher.publish(show["id"], published)
    scheduled = episode(guid="future-id")
    publisher.publish(show["id"], scheduled, datetime.now(timezone.utc) + timedelta(days=10))
    before = db.list_episodes(show["id"])
    path = tmp_path / "episodes.csv"
    export_csv(db, show["id"], path)
    assert import_csv(db, show["id"], path) == 2
    assert db.list_episodes(show["id"]) == before
    for selection in ("published", "scheduled"):
        export_csv(db, show["id"], path, selection)
        rows = list(csv.DictReader(path.open()))
        assert len(rows) == 1 and rows[0]["status"] == selection
        assert rows[0]["guid"]
    write_csv(path, [{"guid": "original-id", "title": "Edited"}], ("guid", "title"))
    import_csv(db, show["id"], path)
    assert len(db.list_episodes(show["id"])) == 2
    assert next(e for e in db.list_episodes(show["id"]) if e["guid"] == "original-id")["title"] == "Edited"


def test_csv_missing_guid_exact_url_and_ambiguity(setup, tmp_path):
    db, show, publisher = setup
    publisher.publish(show["id"], episode(guid="opaque"))
    path = tmp_path / "episodes.csv"
    write_csv(path, [{"mp3_url": "https://example.org/audio.mp3", "title": "Matched"}], ("mp3_url", "title"))
    import_csv(db, show["id"], path)
    assert db.list_episodes(show["id"])[0]["guid"] == "opaque"
    publisher.publish(show["id"], episode(guid="second"))
    with pytest.raises(ValueError, match="Ambiguous"):
        import_csv(db, show["id"], path)
    assert len(db.list_episodes(show["id"])) == 2


def test_csv_full_preflight_and_recovery(setup, tmp_path, monkeypatch):
    db, show, publisher = setup
    publisher.publish(show["id"], episode(guid="one"))
    publisher.publish(show["id"], episode(guid="two"))
    before = db.list_episodes(show["id"])
    path = tmp_path / "episodes.csv"
    write_csv(path, [{"guid": "one", "title": "Updated"}, {"guid": "two", "title": "x" * 61}], ("guid", "title"))
    with pytest.raises(ValueError, match="title"):
        import_csv(db, show["id"], path)
    assert db.list_episodes(show["id"]) == before
    write_csv(path, [{"guid": "one", "title": "Updated"}, {"guid": "two", "title": "Also updated"}], ("guid", "title"))
    with monkeypatch.context() as patch:
        patch.setattr("termicast.publisher.atomic_write", Mock(side_effect=OSError("disk failure")))
        with pytest.raises(OSError):
            import_csv(db, show["id"], path)
    with db.connection() as conn:
        assert conn.execute("SELECT dirty FROM shows").fetchone()[0] == 1
    publisher.publish_due()
    feed = etree.parse(str(Path(show["output_dir"]) / "feed.xml"))
    assert set(feed.xpath("//item/title/text()")) == {"Updated", "Also updated"}


def test_only_unreleased_can_reschedule(setup):
    db, show, publisher = setup
    publisher.publish(show["id"], episode(guid="one"))
    saved = db.list_episodes(show["id"])[0]
    future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    with pytest.raises(ValueError, match="cannot be rescheduled"):
        publisher.merge(show["id"], [saved | {"publish_at": future, "status": "scheduled"}])
    publisher.publish(show["id"], episode(guid="two"), datetime.now(timezone.utc) + timedelta(days=1))
    saved = next(e for e in db.list_episodes(show["id"]) if e["guid"] == "two")
    publisher.merge(show["id"], [saved | {"publish_at": future}])
    assert db.list_episodes(show["id"])[0]["publish_at"] == future


def test_download_import_and_edit_preserve_xml(tmp_path, monkeypatch):
    source = Path(__file__).parent / "fixtures/sample.xml"
    settings, original = import_feed(str(source))
    root = etree.fromstring(original)
    item = root.find("channel/item")
    etree.SubElement(item, "{" + NS["podcast"] + "}chapters", url="https://old.example/chapters.json", type="application/json+chapters")
    etree.SubElement(item, "{" + NS["podcast"] + "}transcript", url="https://old.example/text.vtt", type="text/vtt", language="en")
    etree.SubElement(item, "{" + NS["itunes"] + "}image", href="https://old.example/episode.png")
    original = etree.tostring(root)
    settings.update(output_dir=str(tmp_path / "public"), base_url="https://new.example/show")
    image = BytesIO()
    Image.new("RGB", (3000, 3000)).save(image, "PNG")
    downloaded = []
    def download(url, target, limit):
        downloaded.append(url)
        if url.endswith(".json"):
            target.write(json.dumps({"version": "1.2.0", "custom": "keep", "chapters": [dict(startTime=0, title="Start", img="https://old.example/chapter.png")]}).encode())
        elif url.endswith((".jpg", ".png")):
            target.write(image.getvalue())
        elif url.endswith(".vtt"):
            target.write(b"WEBVTT\n\n00:00.000 --> 00:01.000\nTranscript\n")
        else:
            target.write(b"audio or transcript")
    monkeypatch.setattr(validation, "_download", download)
    probe = Mock(return_value={"length": 19, "duration": 3723})
    monkeypatch.setattr(validation, "probe_local_media", probe)
    show, episodes = download_import(settings, original)
    probe.assert_called_once()
    assert len(downloaded) == len(set(downloaded)) == 6
    db = Database(tmp_path / "state/termicast.db")
    db.save_show(show, original, episodes)
    publisher = Publisher(db)
    publisher.regenerate(show["id"])
    saved = db.list_episodes(show["id"])[0]
    assert saved["guid"] == "rss-host-original-id-42"
    assert saved["mp3_url"].startswith(show["base_url"] + "/audio/")
    publisher.merge(show["id"], [saved | {"title": "Edited imported episode"}])
    rendered = etree.parse(str(Path(show["output_dir"]) / "feed.xml"))
    assert len(rendered.findall("channel/item")) == 1
    assert rendered.findtext("channel/item/{https://example.org/extensions}episodeData") == "retain this"
    assert rendered.findtext("channel/item/content:encoded", namespaces=NS) == "<p>Full original text.</p>"
    transcript = rendered.find("channel/item/podcast:transcript", NS)
    assert transcript.get("language") == "en"
    assert transcript.get("url").startswith(show["base_url"] + "/transcripts/")
    assert saved["chapters"][0]["img"].startswith(show["base_url"] + "/images/chapters/")
    with db.connection() as conn:
        assert conn.execute("SELECT template FROM shows").fetchone()[0] == original
    assert not list(tmp_path.glob(".termicast-import-*"))


def test_failed_import_cleans_staging(tmp_path, monkeypatch):
    show, original = import_feed(str(Path(__file__).parent / "fixtures/sample.xml"))
    show.update(output_dir=str(tmp_path / "public"), base_url="https://new.example/show")
    monkeypatch.setattr(validation, "_download", Mock(side_effect=OSError("network failure")))
    with pytest.raises(OSError):
        download_import(show, original)
    assert not (tmp_path / "public").exists()
    assert not list(tmp_path.glob(".termicast-import-*"))


def test_backup_media_and_exclusions(setup, tmp_path):
    db, show, publisher = setup
    publisher.regenerate(show["id"])
    root = Path(show["output_dir"])
    (root / "audio").mkdir()
    (root / "audio/file.mp3").write_bytes(b"audio")
    (root / "private.db").write_bytes(b"secret")
    with ZipFile(create_backup(db, include_media=True)) as archive:
        assert f"outputs/{show['id']}/audio/file.mp3" in archive.namelist()
        assert not any(name.endswith("private.db") for name in archive.namelist())
    (root / "audio/link").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symbolic-link"):
        create_backup(db, include_media=True)
    with pytest.raises(ValueError, match="public"):
        create_backup(db, root / "backups", include_media=True)


@pytest.mark.parametrize("mode,format_name", [("RGBA", "PNG"), ("L", "PNG"), ("RGB", "GIF")])
def test_artwork_color_and_format(tmp_path, mode, format_name):
    path = tmp_path / "art"
    Image.new(mode, (3000, 3000)).save(path, format_name)
    with pytest.raises(ValueError, match="JPEG or PNG in RGB"):
        validation.inspect_local_artwork(path)


def test_download_disk_check_and_truncation(tmp_path, monkeypatch):
    class Response(BytesIO):
        headers = {"Content-Length": "100"}
    monkeypatch.setattr(validation, "_open", lambda *a: Response(b"short"))
    with pytest.raises(ValueError, match="Incomplete"):
        validation._download("https://example.org/file", BytesIO(), 100)
    monkeypatch.setattr(validation.shutil, "disk_usage", lambda *a: type("Disk", (), {"free": 0})())
    with (tmp_path / "file").open("wb") as target:
        with pytest.raises(OSError, match="disk space"):
            validation._download("https://example.org/file", target, 100)


def test_legacy_schema_migration(tmp_path):
    path = tmp_path / "legacy.db"
    show = new_show(title="Legacy", description="Legacy", output_dir=str(tmp_path / "public"),
                    base_url="https://example.org/legacy")
    saved = episode(guid="opaque") | {"published_at": "2000-01-01T00:00:00+00:00"}
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE shows (id TEXT PRIMARY KEY, settings TEXT, output_dir TEXT UNIQUE, template BLOB, dirty INTEGER DEFAULT 1)")
        conn.execute("INSERT INTO shows (id, settings, output_dir) VALUES (?, ?, ?)",
                     (show["id"], json.dumps(show), show["output_dir"]))
        conn.execute("CREATE TABLE episodes (guid TEXT PRIMARY KEY, show_id TEXT, data TEXT, publish_at TEXT, status TEXT)")
        conn.execute("INSERT INTO episodes VALUES (?, ?, ?, ?, 'published')",
                     (saved["guid"], show["id"], json.dumps(saved), saved["published_at"]))
    db = Database(path)
    assert db.list_episodes(show["id"])[0]["guid"] == "opaque"
    assert db.list_episodes(show["id"])[0]["title"] == "Episode"
    with db.connection() as conn:
        assert {r["name"]: r["pk"] for r in conn.execute("PRAGMA table_info(episodes)")}["show_id"] == 1


def test_new_csv_optional_guid_and_timezone(setup, tmp_path):
    db, show, publisher = setup
    path = tmp_path / "episodes.csv"
    row = dict(episode(), guid="", published_at="2099-01-01T09:00:00-05:00", timezone="America/New_York")
    for key in ("keywords", "chapters", "soundbites"):
        row[key] = json.dumps(row[key])
    write_csv(path, [row])
    assert import_csv(db, show["id"], path) == 1
    saved = db.list_episodes(show["id"])[0]
    assert saved["guid"] and saved["status"] == "scheduled"
    assert saved["publish_at"] == "2099-01-01T14:00:00+00:00"
    row["guid"] = saved["guid"]
    row["published_at"] = "2099-01-01T09:00:00Z"
    write_csv(path, [row])
    with pytest.raises(ValueError, match="offset inconsistent"):
        import_csv(db, show["id"], path)


def test_batch_new_publication_recovery_and_no_unrelated_release(setup, monkeypatch):
    db, show, publisher = setup
    now = datetime.now(timezone.utc)
    publisher.publish(show["id"], episode(guid="unrelated"), now + timedelta(days=1))
    with db.connection() as conn:
        conn.execute("UPDATE episodes SET publish_at=? WHERE guid='unrelated'", ((now - timedelta(days=1)).isoformat(),))
    publisher.merge(show["id"], [episode(guid="new")])
    assert {e["guid"]: e["status"] for e in db.list_episodes(show["id"])} == {"new": "published", "unrelated": "scheduled"}
    with monkeypatch.context() as patch:
        patch.setattr("termicast.publisher.atomic_write", Mock(side_effect=OSError("disk failure")))
        with pytest.raises(OSError):
            publisher.merge(show["id"], [episode(guid="recover")])
    assert next(e for e in db.list_episodes(show["id"]) if e["guid"] == "recover")["status"] == "scheduled"
    publisher.publish_due()
    assert all(e["status"] == "published" for e in db.list_episodes(show["id"]))


@pytest.mark.parametrize("rows", [
    [{"guid": "one", "title": "A"}, {"guid": "one", "title": "B"}],
    [{"guid": "one", "chapters": "not JSON"}],
])
def test_csv_invalid_batch_is_not_partially_saved(setup, tmp_path, rows):
    db, show, publisher = setup
    publisher.publish(show["id"], episode(guid="one"))
    before = db.list_episodes(show["id"])
    path = tmp_path / "episodes.csv"
    fields = tuple(rows[0])
    write_csv(path, rows, fields)
    with pytest.raises(ValueError):
        import_csv(db, show["id"], path)
    assert db.list_episodes(show["id"]) == before


def test_csv_template_and_private_destinations(setup):
    db, show, publisher = setup
    template = Path(__file__).parents[1] / "episode_template.csv"
    with template.open() as handle:
        assert next(csv.reader(handle)) == list(FIELDS)
    for target in (db.path, Path(show["output_dir"]) / "export.csv"):
        with pytest.raises(ValueError):
            export_csv(db, show["id"], target)


def test_saved_editor_keeps_guid_and_published_time(setup, monkeypatch):
    db, show, publisher = setup
    publisher.publish(show["id"], episode(guid="opaque"))
    saved = db.list_episodes(show["id"])[0]
    choices = iter([1, 2])
    def menu(title, options, default=None):
        assert "Reschedule" not in options and "Publish now" not in options
        return next(choices)
    monkeypatch.setattr(prompts, "menu", menu)
    monkeypatch.setattr(prompts, "edit_menu", lambda data, *a, **k: data.update(title="Edited"))
    monkeypatch.setattr(prompts, "confirm", lambda *a: True)
    prompts.edit_episode_form(show, publisher, saved)
    updated = db.list_episodes(show["id"])[0]
    assert updated["title"] == "Edited"
    assert updated["guid"] == saved["guid"]
    assert updated["publish_at"] == saved["publish_at"]
    assert saved["title"] == "Episode"


def test_chapter_editor_preserves_extra_metadata(monkeypatch):
    old = dict(startTime=0, endTime=120, title="Old", custom="keep", img="https://example.org/old.png")
    monkeypatch.setattr(prompts, "menu", Mock(side_effect=[2, 1, 4]))
    monkeypatch.setattr(prompts, "number", Mock(side_effect=[0, 120]))
    monkeypatch.setattr(prompts, "text", Mock(side_effect=["New", "https://example.org/new.png", "https://example.org/chapter"]))
    result = prompts._segments([old], False)
    assert result[0]["img"] == "https://example.org/new.png"
    assert result[0]["custom"] == "keep"
    assert old["title"] == "Old"


@pytest.mark.parametrize("missing", [True, False])
def test_rehosting_never_changes_episode_identity(tmp_path, monkeypatch, missing):
    from termicast.importer import extract_episodes
    settings, original = import_feed(str(Path(__file__).parent / "fixtures/sample.xml"))
    root = etree.fromstring(original)
    item = root.find("channel/item")
    url = item.find("enclosure").get("url")
    if missing:
        item.remove(item.find("guid"))
    else:
        item.find("guid").text = url
    original = etree.tostring(root)
    saved = extract_episodes(original)[0]
    mapping = {url: "https://new.example/audio/new.mp3"}
    settings.update(output_dir=str(tmp_path / "public"), base_url="https://new.example", import_url_map=mapping)
    saved["mp3_url"] = mapping[url]
    db = Database(tmp_path / "state/termicast.db")
    db.save_show(settings, original, [saved])
    Publisher(db).regenerate(settings["id"])
    assert len(db.list_episodes(settings["id"])) == 1
    rendered = etree.parse(str(tmp_path / "public/feed.xml"))
    assert rendered.xpath("//item/guid/text()") == [saved["guid"]]


def test_shared_chapter_payload_is_downloaded_once(tmp_path, monkeypatch):
    from copy import deepcopy
    settings, original = import_feed(str(Path(__file__).parent / "fixtures/sample.xml"))
    root = etree.fromstring(original)
    item = root.find("channel/item")
    etree.SubElement(item, "{" + NS["podcast"] + "}chapters", url="https://old.example/chapters.json")
    other = deepcopy(item)
    other.find("guid").text = "other-id"
    root.find("channel").append(other)
    original = etree.tostring(root)
    settings.update(output_dir=str(tmp_path / "public"), base_url="https://new.example")
    image = BytesIO()
    Image.new("RGB", (3000, 3000)).save(image, "PNG")
    urls = []
    def download(url, target, limit):
        assert url not in urls
        urls.append(url)
        if url.endswith(".json"):
            target.write(b'{"chapters":[{"startTime":0,"title":"Start","img":"https://old.example/image.png"}]}')
        elif url.endswith((".png", ".jpg")):
            target.write(image.getvalue())
        else:
            target.write(b"audio")
    monkeypatch.setattr(validation, "_download", download)
    probe = Mock(return_value={"length": 5, "duration": 3723})
    monkeypatch.setattr(validation, "probe_local_media", probe)
    show, episodes = download_import(settings, original)
    assert len(episodes) == 2
    assert len(urls) == 4
    probe.assert_called_once()
    assert episodes[0]["chapters"] == episodes[1]["chapters"]


def test_scheduled_xml_is_preflighted_before_commit(setup):
    db, show, publisher = setup
    bad = episode(guid="invalid") | {"title": "Title\x01", "published_at": "2099-01-01T00:00:00+00:00"}
    with pytest.raises(ValueError):
        publisher.merge(show["id"], [bad])
    assert db.list_episodes(show["id"]) == []
