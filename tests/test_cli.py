from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, call
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo

from lxml import etree
import pytest

from termicast import cli, prompts
from termicast.database import Database
from termicast.feed import NS, validate_feed
from termicast.models import new_episode, new_show
from termicast.publisher import Publisher
from termicast.importer import extract_episodes


FIXTURE = Path(__file__).parent / "fixtures" / "sample.xml"


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    home = tmp_path / "state"
    monkeypatch.setenv("TERMICAST_HOME", str(home))
    return home


@pytest.fixture
def db():
    return Database()


@pytest.fixture
def show(db, tmp_path):
    record = new_show(
        title="CLI podcast", description="A test podcast", author="Test author",
        owner_name="Test owner", owner_email="owner@example.org",
        website="https://example.org/show", category="Technology",
        base_url="https://example.org/show", output_dir=str(tmp_path / "output"),
        timezone="America/New_York", explicit=True,
    )
    db.save_show(record)
    return record


def test_help_does_not_create_database(data_dir, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--data-dir" in output
    assert "publish-due" in output
    assert "validate" in output
    assert not data_dir.exists()


def test_empty_publish_due_uses_explicit_data_dir(tmp_path, data_dir, capsys):
    selected = tmp_path / "selected"
    assert cli.main(["--data-dir", str(selected), "publish-due"]) == 0
    assert "Published 0 due episode(s)." in capsys.readouterr().out
    assert (selected / "termicast.db").is_file()
    assert not data_dir.exists()
    assert Database().list_shows() == []


def test_validate_empty(capsys):
    assert cli.main(["validate"]) == 0
    assert "No managed feeds to validate." in capsys.readouterr().out


@pytest.mark.parametrize("all_shows", [False, True])
def test_validate_valid_feed(db, show, capsys, all_shows):
    Publisher(db).regenerate(show["id"])
    assert cli.main(["validate"] + ([] if all_shows else [show["id"]])) == 0
    output = capsys.readouterr().out
    assert show["id"] in output
    assert ": valid (" in " ".join(output.split())
    assert "FAILED" not in output


@pytest.mark.parametrize("contents", [None, b"not XML", b"<rss><channel/></rss>"])
def test_validate_invalid_or_missing_feed(db, show, capsys, contents):
    if contents is not None:
        path = Path(show["output_dir"]) / "feed.xml"
        path.parent.mkdir()
        path.write_bytes(contents)
    assert cli.main(["validate", show["id"]]) == 1
    output = capsys.readouterr().out
    assert show["id"] in output
    assert "FAILED" in output
    if contents is None:
        assert "Cannot validate managed feed" in output


def test_validate_unknown_show(show, capsys):
    assert cli.main(["validate", "unknown-show"]) == 1
    assert "Unknown podcast ID: unknown-show" in capsys.readouterr().out


def test_validate_all_continues_after_a_bad_feed(db, show, tmp_path, capsys):
    healthy = show | {"id": str(uuid4()), "title": "Healthy podcast",
                      "output_dir": str(tmp_path / "healthy")}
    db.save_show(healthy)
    Publisher(db).regenerate(healthy["id"])
    assert cli.main(["validate"]) == 1
    output = " ".join(capsys.readouterr().out.split())
    assert f"({show['id']}): FAILED" in output
    assert f"({healthy['id']}): valid" in output


@pytest.mark.parametrize("exception, status, message", [
    (EOFError, 0, "Input closed. Exiting."),
    (KeyboardInterrupt, 130, "Cancelled. Exiting."),
])
def test_interactive_input_exit(monkeypatch, capsys, exception, status, message):
    monkeypatch.setattr(prompts.console, "input", Mock(side_effect=exception))
    assert cli.main([]) == status
    assert message in capsys.readouterr().out


def test_eof_during_create_exits_without_returning_to_menu(monkeypatch, capsys):
    read = Mock(side_effect=["3", EOFError(), "5"])
    monkeypatch.setattr(prompts.console, "input", read)
    assert cli.main([]) == 0
    assert "Input closed. Exiting." in capsys.readouterr().out
    assert read.call_count == 2


def test_import_preserves_identity_and_xml(db, tmp_path, monkeypatch, capsys):
    original = FIXTURE.read_bytes()
    destination = {"output_dir": str(tmp_path / "imported"),
                   "base_url": "https://new.example.org/podcast"}
    source = Mock(return_value=str(FIXTURE))
    edit = Mock(side_effect=lambda data, field: data.update({field: destination[field]}))
    form = Mock(side_effect=lambda data: data)
    monkeypatch.setattr(cli, "text", source)
    monkeypatch.setattr(cli, "edit_field", edit)
    monkeypatch.setattr(cli, "show_form", form)
    monkeypatch.setattr(cli, "download_import", lambda show, template, **kwargs: (show, extract_episodes(template)))

    cli._create_or_import(db, Publisher(db), importing=True)

    source.assert_called_once_with("Existing feed (HTTPS URL or local XML path)", required=True)
    assert [args.args[1] for args in edit.call_args_list] == ["output_dir", "base_url"]
    form.assert_called_once()
    saved, = db.list_shows()
    assert saved["guid"] == "5eaf7b5e-cc24-5e12-9c73-96c1f81c3b12"
    assert saved["title"] == "Representative & Friends"
    assert saved["base_url"] == destination["base_url"]
    assert db.list_episodes(saved["id"])[0]["guid"] == "rss-host-original-id-42"
    with db.connection() as conn:
        assert conn.execute("SELECT template FROM shows").fetchone()[0] == original
    rendered = (Path(saved["output_dir"]) / "feed.xml").read_bytes()
    assert validate_feed(rendered) == []
    before, after = etree.fromstring(original), etree.fromstring(rendered)
    assert after.findtext("channel/podcast:guid", namespaces=NS) == saved["guid"]
    assert after.find("channel/atom:link[@rel='self']", NS).get("href") == destination["base_url"] + "/feed.xml"
    for path in ("channel/item", "channel/podcast:value", "channel/podcast:podroll",
                 "channel/{https://example.org/extensions}metadata"):
        old, new = before.find(path, NS), after.find(path, NS)
        assert [(e.tag, dict(e.attrib), e.text) for e in new.iter()] == [
            (e.tag, dict(e.attrib), e.text) for e in old.iter()
        ]
    assert [c.text for c in after.xpath("//comment()")] == [c.text for c in before.xpath("//comment()")]
    assert "Podcast saved and feed generated." in capsys.readouterr().out


def test_create_cancel_has_no_side_effects(db, monkeypatch):
    form = Mock(return_value=None)
    publisher = Mock(spec=Publisher)
    monkeypatch.setattr(cli, "show_form", form)
    cli._create_or_import(db, publisher)
    assert form.call_args.kwargs == {"collect": True}
    assert db.list_shows() == []
    publisher.regenerate.assert_not_called()


@pytest.mark.parametrize("collect", [True, False])
def test_show_form_final_url_guid_and_existing_identity(show, monkeypatch, collect):
    original = new_show() if collect else deepcopy(show)
    untouched = deepcopy(original)
    final_url = "https://final.example.org/podcast/"
    edits = []

    def edit(data, field, episode=False):
        edits.append(field)
        if field == "base_url" and edits.count(field) == (2 if collect else 1):
            data[field] = final_url
        else:
            data[field] = deepcopy(show[field])

    choices = Mock(side_effect=[2, prompts.SHOW_FIELDS.index("base_url") + 1, 1])
    monkeypatch.setattr(prompts, "edit_field", edit)
    monkeypatch.setattr(prompts, "menu", choices)
    result = prompts.show_form(original, collect=collect)
    assert original == untouched
    assert result["base_url"] == final_url
    assert result["id"] == original["id"]
    expected = str(uuid5(NAMESPACE_URL, final_url.rstrip("/") + "/feed.xml"))
    assert result["guid"] == (expected if collect else original["guid"])
    assert edits == (list(prompts.SHOW_ESSENTIALS) if collect else []) + ["base_url"]
    assert choices.call_count == 3


@pytest.mark.parametrize("action", ["cancel", "decline", "cancel-schedule", "publish", "schedule"])
def test_episode_review(db, show, monkeypatch, capsys, action):
    fields = new_episode(title="CLI episode", description="Episode description",
                         mp3_url="https://example.org/episode.mp3", length=123456,
                         duration=120, explicit=show["explicit"])
    edit = Mock(side_effect=lambda data, field, episode: data.update({field: deepcopy(fields[field])}))
    selection = {"cancel": [4], "decline": [2, 4], "cancel-schedule": [3, 4],
                 "publish": [2], "schedule": [3]}[action]
    monkeypatch.setattr(prompts, "edit_field", edit)
    monkeypatch.setattr(prompts, "menu", Mock(side_effect=selection))
    confirmation = Mock(return_value=action != "decline")
    monkeypatch.setattr(prompts, "confirm", confirmation)
    target = (datetime.now(timezone.utc) + timedelta(days=7)).replace(microsecond=0)
    local = target.astimezone(ZoneInfo(show["timezone"]))
    monkeypatch.setattr(prompts, "text", Mock(return_value="" if action == "cancel-schedule" else local.isoformat()))
    publisher = Publisher(db)
    publisher.regenerate(show["id"])
    feed = Path(show["output_dir"]) / "feed.xml"
    before = feed.read_bytes()

    prompts.episode_form(show, publisher)

    assert [c.args[1] for c in edit.call_args_list] == list(prompts.EPISODE_ESSENTIALS) + ["length", "duration"]
    assert all(c.kwargs == {"episode": True} for c in edit.call_args_list)
    episodes = db.list_episodes(show["id"])
    if action not in ("publish", "schedule"):
        assert episodes == []
        assert feed.read_bytes() == before
        if action != "decline":
            confirmation.assert_not_called()
        return
    saved, = episodes
    assert {key: saved[key] for key in prompts.EPISODE_FIELDS} == {key: fields[key] for key in prompts.EPISODE_FIELDS}
    assert validate_feed(feed.read_bytes()) == []
    output = capsys.readouterr().out
    if action == "schedule":
        assert saved["status"] == "scheduled"
        assert saved["publish_at"] == target.isoformat()
        assert feed.read_bytes() == before
        assert "UTC:" in output
        assert f"Scheduled episode {saved['guid']}" in output
        assert confirmation.call_args_list == [call("Schedule this episode?")]
    else:
        assert saved["status"] == "published"
        assert etree.fromstring(feed.read_bytes()).findtext("channel/item/guid") == saved["guid"]
        assert f"Published episode {saved['guid']}" in output
        confirmation.assert_called_once_with("Publish this episode now?")


@pytest.mark.parametrize("probe_result", [OSError("offline"), {}, {"length": 54321}])
def test_media_probe_falls_back_to_manual_fields(monkeypatch, capsys, probe_result):
    episode = new_episode(mp3_url="https://example.org/old.mp3", length=999, duration=99)
    probe = Mock(side_effect=probe_result) if isinstance(probe_result, Exception) else Mock(return_value=probe_result)
    monkeypatch.setattr(prompts, "probe_media", probe)
    monkeypatch.setattr(prompts, "confirm", Mock(return_value=True))
    monkeypatch.setattr(prompts, "text", Mock(side_effect=["https://example.org/new.mp3", "123456", "02:30"]))
    prompts.edit_field(episode, "mp3_url", episode=True)
    assert episode["length"] == (54321 if probe_result == {"length": 54321} else None)
    assert episode["duration"] is None
    assert "manually" in capsys.readouterr().out
    prompts.edit_field(episode, "length", episode=True)
    prompts.edit_field(episode, "duration", episode=True)
    assert episode["length"] == 123456
    assert episode["duration"] == 150
    probe.assert_called_once_with("https://example.org/new.mp3")


def test_artwork_inspection_can_be_skipped(monkeypatch):
    inspect = Mock()
    monkeypatch.setattr(prompts, "inspect_artwork", inspect)
    monkeypatch.setattr(prompts, "text", Mock(return_value="https://example.org/art.jpg"))
    monkeypatch.setattr(prompts, "confirm", Mock(return_value=False))
    assert prompts._artwork("", episode=True) == "https://example.org/art.jpg"
    inspect.assert_not_called()


@pytest.mark.parametrize("metadata,missing", [
    ({"length": 1234, "duration": 60}, []),
    ({"length": 1234}, ["duration"]),
])
def test_short_episode_flow_keeps_probe_results(show, monkeypatch, metadata, missing):
    fields = []
    def edit(data, field, episode=False):
        fields.append(field)
        if field == "mp3_url":
            data.update(mp3_url="https://example.org/audio.mp3", **metadata)
        elif field == "duration":
            data[field] = 60
        else:
            data[field] = "Example"
    monkeypatch.setattr(prompts, "edit_field", edit)
    monkeypatch.setattr(prompts, "menu", Mock(side_effect=[5, 2]))
    def assets(episode, show):
        episode["chapters"] = [{"startTime": 0, "endTime": 60, "title": "Opening"}]
    monkeypatch.setattr(prompts, "optional_assets", assets)
    monkeypatch.setattr(prompts, "confirm", Mock(return_value=True))
    publisher = Mock()
    prompts.episode_form(show, publisher)
    assert fields == ["title", "description", "mp3_url"] + missing
    saved = publisher.publish.call_args.args[1]
    assert saved["length"] == 1234
    assert saved["duration"] == 60
    assert saved["chapters"][0]["title"] == "Opening"


def test_episode_browser_filters_and_edits(show, monkeypatch):
    published = new_episode(title="Released", status="published")
    scheduled = new_episode(title="Upcoming", status="scheduled")
    db = Mock()
    db.list_episodes.return_value = [published, scheduled]
    # Filter -> scheduled -> select the remaining episode -> back.
    monkeypatch.setattr(cli, "menu", Mock(side_effect=[3, 3, 1, 3]))
    editor = Mock()
    monkeypatch.setattr(cli, "edit_episode_form", editor)
    publisher = Mock()
    cli._episodes(db, publisher, show)
    editor.assert_called_once_with(show, publisher, scheduled)
