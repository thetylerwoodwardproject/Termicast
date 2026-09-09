import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import Mock
from zipfile import ZipFile

from lxml import etree
import pytest

from termicast import prompts, repair, validation
from termicast.assets import check_vtt, read_chapters
from termicast.csvio import import_csv
from termicast.database import Database
from termicast.feed import NS
from termicast.importer import download_import, import_feed
from termicast.models import chapter_filename, new_episode, new_show
from termicast.publisher import Publisher


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TERMICAST_HOME", str(tmp_path / "state"))
    db = Database()
    show = new_show(title="Show", description="Description", base_url="https://new.example/show",
                    output_dir=str(tmp_path / "public"))
    db.save_show(show)
    publisher = Publisher(db)
    episode = new_episode(title="Episode", description="Description", length=12, duration=120,
                          mp3_url="https://new.example/show/audio/episode.mp3",
                          chapters=[dict(startTime=0, endTime=120, title="Start")])
    publisher.publish(show["id"], episode)
    return db, show, publisher, episode


def long_title(db, show, guid):
    with db.connection() as conn:
        row = conn.execute("SELECT data FROM episodes WHERE guid=?", (guid,)).fetchone()
        data = json.loads(row[0])
        data["title"] = "A" * 59 + " " + "extra"
        conn.execute("UPDATE episodes SET data=? WHERE guid=?", (json.dumps(data), guid))


def test_scan_read_only_and_repair_preserves_schedule_backup(saved):
    db, show, publisher, episode = saved
    future = dict(episode, guid="scheduled-id", chapters=[])
    publisher.publish(show["id"], future, datetime.now(timezone.utc) + timedelta(days=1))
    # Even an overdue scheduled row must remain scheduled during repair.
    with db.connection() as conn:
        conn.execute("UPDATE episodes SET publish_at='2000-01-01T00:00:00+00:00' WHERE guid='scheduled-id'")
    long_title(db, show, episode["guid"])
    before = db.list_episodes(show["id"])
    output = Path(show["output_dir"])
    old_feed = (output / "feed.xml").read_bytes()
    (output / "chapters" / chapter_filename(episode["guid"])).unlink()
    scan = repair.scan_show(db, show["id"])
    assert db.list_episodes(show["id"]) == before
    assert (output / "feed.xml").read_bytes() == old_feed
    assert not (db.path.parent / "backups").exists()
    backup, after = repair.repair_show(db, scan, [episode["guid"]])
    with ZipFile(backup) as archive:
        assert archive.read(f"outputs/{show['id']}/feed.xml") == old_feed
        assert "termicast.db" in archive.namelist()
    rows = {e["guid"]: e for e in after["episodes"]}
    assert rows[episode["guid"]]["title"] == "A" * 59
    for old in before:
        assert rows[old["guid"]]["publish_at"] == old["publish_at"]
        assert rows[old["guid"]]["published_at"] == old["published_at"]
        assert rows[old["guid"]]["status"] == old["status"]
    assert b"scheduled-id" not in (output / "feed.xml").read_bytes()
    assert (output / "chapters" / chapter_filename(episode["guid"])).exists()
    assert not after["fixes"]


def test_failed_backup_prevents_repairs(saved, monkeypatch):
    db, show, publisher, episode = saved
    long_title(db, show, episode["guid"])
    scan = repair.scan_show(db, show["id"])
    monkeypatch.setattr(repair, "create_backup", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        repair.repair_show(db, scan, [episode["guid"]])
    assert db.list_episodes(show["id"]) == scan["episodes"]


def test_stale_scan_rejected(saved):
    db, show, publisher, episode = saved
    scan = repair.scan_show(db, show["id"])
    long_title(db, show, episode["guid"])
    with pytest.raises(ValueError, match="changed since scan"):
        repair.repair_show(db, scan, [])


def test_missing_feed_and_media_recovery_new_output(saved, tmp_path):
    db, show, publisher, episode = saved
    (Path(show["output_dir"]) / "feed.xml").unlink()
    scan = repair.scan_show(db, show["id"])
    source = tmp_path / "original.mp3"
    source.write_bytes(b"recovered audio")
    missing = next(m for m in scan["missing"] if m["url"].endswith(".mp3"))
    target = tmp_path / "corrected"
    _, result = repair.repair_show(db, scan, [], {missing["path"]: str(source)}, str(target))
    assert (target / "audio/episode.mp3").read_bytes() == source.read_bytes()
    assert (target / "feed.xml").exists()
    assert result["show"]["base_url"] == show["base_url"]
    assert not result["missing"]


def test_failed_regeneration_keeps_dirty_intent(saved, monkeypatch):
    db, show, publisher, episode = saved
    long_title(db, show, episode["guid"])
    scan = repair.scan_show(db, show["id"])
    monkeypatch.setattr(Publisher, "_write", Mock(side_effect=OSError("replace failed")))
    with pytest.raises(OSError):
        repair.repair_show(db, scan, [episode["guid"]])
    with db.connection() as conn:
        assert conn.execute("SELECT dirty FROM shows").fetchone()[0] == 1
    assert db.list_episodes(show["id"])[0]["title"] == "A" * 59


@pytest.mark.parametrize("approve", [False, True])
def test_csv_title_preview_is_atomic(saved, tmp_path, approve):
    db, show, publisher, episode = saved
    path = tmp_path / "titles.csv"
    with path.open("w") as handle:
        writer = csv.writer(handle)
        writer.writerows([['guid', 'title'], [episode['guid'], 'T' * 61]])
    review = Mock(return_value=approve)
    if approve:
        assert import_csv(db, show['id'], path, review_titles=review) == 1
    else:
        with pytest.raises(ValueError, match="confirmation"):
            import_csv(db, show['id'], path, review_titles=review)
    assert review.call_args.args[0] == [(episode['guid'], 'T' * 61, 'T' * 60)]
    assert db.list_episodes(show['id'])[0]['title'] == ('T' * 60 if approve else 'Episode')


def imported(tmp_path, monkeypatch, linked=False):
    show, original = import_feed(str(Path(__file__).parent / "fixtures/sample.xml"))
    root = etree.fromstring(original)
    for image in root.findall("channel/itunes:image", NS) + root.findall("channel/image"):
        image.getparent().remove(image)
    show.update(artwork_url="", output_dir=str(tmp_path / "imported"), base_url="https://new.example/show")
    if linked:
        etree.SubElement(root.find("channel/item"), "{" + NS['podcast'] + "}transcript",
                         url="https://old.example/broken.vtt", type="text/vtt")
    monkeypatch.setattr(validation, "probe_local_media", lambda path: dict(length=12, duration=3723))
    return show, etree.tostring(root)


def test_absent_optional_resources_no_downloads_or_empty_files(tmp_path, monkeypatch):
    show, template = imported(tmp_path, monkeypatch)
    calls = []
    def download(url, handle, limit):
        calls.append(url)
        handle.write(b"audio")
    monkeypatch.setattr(validation, "_download", download)
    review = Mock()
    show, episodes = download_import(show, template, review_optional=review)
    assert len(calls) == 1
    assert review.call_count == 1
    assert not episodes[0]['chapters'] and not episodes[0]['transcript_url']
    assert not list((Path(show['output_dir']) / 'chapters').iterdir())
    assert not list((Path(show['output_dir']) / 'transcripts').iterdir())


@pytest.mark.parametrize("action", ['retry', 'replace', 'skip'])
def test_broken_optional_retry_replace_skip(tmp_path, monkeypatch, action):
    show, template = imported(tmp_path, monkeypatch, linked=True)
    attempts = []
    def download(url, handle, limit):
        if url.endswith('.vtt'):
            attempts.append(url)
            if len(attempts) == 1:
                handle.write(b"partial")
                raise OSError("interrupted")
            handle.write(b"WEBVTT\n\n00:00.000 --> 00:01.000\nHello\n")
        else:
            handle.write(b"audio")
    monkeypatch.setattr(validation, '_download', download)
    resolve = lambda e, kind, url, exc: {'retry': url, 'replace': 'https://old.example/replacement.vtt', 'skip': None}[action]
    show, episodes = download_import(show, template, resolve_optional=resolve)
    db = Database(tmp_path / 'state/db')
    db.save_show(show, template=template, episodes=episodes)
    Publisher(db).regenerate(show['id'])
    root = etree.parse(str(Path(show['output_dir']) / 'feed.xml'))
    transcript = root.find('channel/item/podcast:transcript', NS)
    if action == 'skip':
        assert transcript is None
        assert not list((Path(show['output_dir']) / 'transcripts').iterdir())
    else:
        assert transcript.get('url').startswith(show['base_url'])
        assert len(attempts) == 2
    with db.connection() as conn:
        assert conn.execute('SELECT template FROM shows').fetchone()[0] == template


def test_local_vtt_review_persists_only_on_save(saved, tmp_path, monkeypatch):
    db, show, publisher, episode = saved
    source = tmp_path / 'source.vtt'
    value = 'WEBVTT\n\n00:00.000 --> 00:01.000\nHello\n'
    source.write_text(value)
    monkeypatch.setattr(prompts, 'menu', lambda *a: 3)
    monkeypatch.setattr(prompts, 'text', lambda *a, **k: str(source))
    monkeypatch.setattr(prompts, 'confirm', lambda *a: True)
    edited = db.list_episodes(show['id'])[0]
    prompts.optional_assets(edited, show)
    target = Path(show['output_dir']) / 'transcripts' / (chapter_filename(episode['guid']) + '.vtt')
    assert not target.exists()
    publisher.merge(show['id'], [edited])
    assert target.read_text() == value
    target.unlink()
    publisher.regenerate(show['id'])
    assert target.read_text() == value


def test_local_chapters_and_invalid_vtt(saved, tmp_path):
    _, _, _, episode = saved
    source = tmp_path / 'chapters.json'
    source.write_text('{"chapters":[{"startTime":0,"title":"Opening"}]}')
    assert read_chapters(str(source), episode)[0]['endTime'] == 120
    with pytest.raises(ValueError):
        check_vtt('')
    with pytest.raises(ValueError):
        check_vtt('WEBVTT\n')


def test_declining_chapter_replacement_keeps_record(saved, tmp_path, monkeypatch):
    from copy import deepcopy
    _, show, _, episode = saved
    source = tmp_path / 'replacement.json'
    source.write_text('{"chapters":[{"startTime":0,"title":"Replacement"}]}')
    before = deepcopy(episode)
    monkeypatch.setattr(prompts, 'menu', lambda *a: 1)
    monkeypatch.setattr(prompts, 'text', lambda *a, **k: str(source))
    monkeypatch.setattr(prompts, 'confirm', lambda *a: False)
    prompts.optional_assets(episode, show)
    assert episode == before


def test_title_save_confirmation_and_edit(saved, monkeypatch):
    _, _, _, episode = saved
    episode['title'] = 'a' * 59 + ' ' + 'extra'
    monkeypatch.setattr(prompts, 'menu', lambda *a: 1)
    prompts.title_for_save(episode)
    assert episode['title'] == 'a' * 59
    episode['title'] = 'x' * 61
    monkeypatch.setattr(prompts, 'menu', lambda *a: 2)
    monkeypatch.setattr(prompts, 'text', lambda *a, **k: 'Edited')
    prompts.title_for_save(episode)
    assert episode['title'] == 'Edited'


@pytest.mark.parametrize('approve', [False, True])
def test_rss_title_review_before_download(tmp_path, monkeypatch, approve):
    show, template = imported(tmp_path, monkeypatch)
    root = etree.fromstring(template)
    root.find('channel/item/title').text = 'Imported ' * 9
    template = etree.tostring(root)
    download = Mock(side_effect=lambda url, handle, limit: handle.write(b'audio'))
    monkeypatch.setattr(validation, '_download', download)
    review = Mock(return_value=approve)
    if approve:
        _, episodes = download_import(show, template, review_titles=review)
        assert episodes[0]['title'] == ('Imported ' * 9)[:60].rstrip()
    else:
        with pytest.raises(ValueError, match='confirmation'):
            download_import(show, template, review_titles=review)
        assert not download.called
        assert not Path(show['output_dir']).exists()
    assert review.call_args.args[0][0][1:] == ('Imported ' * 9, ('Imported ' * 9)[:60].rstrip())


def test_imported_repair_preserves_xml_and_excludes_scheduled(tmp_path):
    show, original = import_feed(str(Path(__file__).parent / 'fixtures/sample.xml'))
    root = etree.fromstring(original)
    item = root.find('channel/item')
    item.find('title').text = 'Imported ' * 9
    etree.SubElement(item, '{https://example.org/custom}unknown', value='keep').text = 'untouched'
    template = etree.tostring(root)
    show.update(output_dir=str(tmp_path / 'public'), base_url='https://new.example/show')
    db = Database(tmp_path / 'state/db')
    db.save_show(show, template=template)
    publisher = Publisher(db)
    publisher.regenerate(show['id'])
    scan = repair.scan_show(db, show['id'])
    guid = scan['episodes'][0]['guid']
    repair.repair_show(db, scan, [guid])
    output = etree.parse(str(Path(show['output_dir']) / 'feed.xml'))
    assert output.find('channel/item/{https://example.org/custom}unknown').text == 'untouched'
    assert output.findtext('channel/item/guid') == guid
    with db.connection() as conn:
        assert conn.execute('SELECT template FROM shows').fetchone()[0] == template
        conn.execute("UPDATE episodes SET status='scheduled' WHERE guid=?", (guid,))
    publisher.regenerate(show['id'])
    assert not etree.parse(str(Path(show['output_dir']) / 'feed.xml')).findall('channel/item')


def test_invalid_linked_chapters_explicit_skip(tmp_path, monkeypatch):
    show, template = imported(tmp_path, monkeypatch)
    root = etree.fromstring(template)
    etree.SubElement(root.find('channel/item'), '{' + NS['podcast'] + '}chapters',
                     url='https://old.example/broken.json')
    template = etree.tostring(root)
    monkeypatch.setattr(validation, '_download',
                        lambda url, handle, limit: handle.write(b'{"chapters":[4]}' if url.endswith('.json') else b'audio'))
    resolve = Mock(return_value=None)
    show, episodes = download_import(show, template, resolve_optional=resolve)
    assert resolve.call_count == 1
    assert not episodes[0]['chapters']
    db = Database(tmp_path / 'state/db')
    db.save_show(show, template=template, episodes=episodes)
    Publisher(db).regenerate(show['id'])
    assert etree.parse(str(Path(show['output_dir']) / 'feed.xml')).find('channel/item/podcast:chapters', NS) is None
    assert not list((Path(show['output_dir']) / 'chapters').iterdir())
