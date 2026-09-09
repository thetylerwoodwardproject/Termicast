from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock
from zipfile import ZipFile

import pytest

from termicast import cli, prompts
from termicast.database import Database
from termicast.models import new_show
from termicast.publisher import Publisher


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    state = tmp_path / "state"
    monkeypatch.setenv("TERMICAST_HOME", str(state))
    token = prompts._backup_action.set(None)
    try:
        yield state
    finally:
        prompts._backup_action.reset(token)


@pytest.fixture
def enter(monkeypatch):
    def install(*answers):
        pending = iter(answers)

        def read(*args, **kwargs):
            try:
                answer = next(pending)
            except StopIteration:
                raise EOFError("Test input exhausted") from None
            if isinstance(answer, BaseException):
                raise answer
            return answer

        reader = Mock(side_effect=read)
        monkeypatch.setattr(prompts.console, "input", reader)
        return reader

    return install


@pytest.fixture
def show(tmp_path):
    return new_show(
        title="Saved podcast", description="Saved description", author="Author",
        owner_name="Owner", owner_email="owner@example.org", category="Technology",
        base_url="https://example.org/podcast", output_dir=str(tmp_path / "public"),
        podroll=[{"feedGuid": "5eaf7b5e-cc24-5e12-9c73-96c1f81c3b12",
                  "feedUrl": "https://example.org/other/feed.xml", "title": "Saved entry"}],
    )


@pytest.mark.parametrize("answer, default, expected", [("2", None, 2), ("", 3, 3)])
def test_shared_menu_utilities_keep_choices_and_default(enter, capsys, answer, default, expected):
    backup = Mock()
    reader = enter("b", "B", "f", "F", answer)
    with prompts.menu_utilities(backup):
        assert prompts.menu("Choose action", ["First", "Second", "Back"], default) == expected
    assert backup.call_count == 2
    assert reader.call_count == 5
    output = capsys.readouterr().out
    assert output.count("Choose action") == 5
    assert output.count("Termicast FAQ") == 2
    for option in ("1. First", "2. Second", "3. Back", "B. Backup saved data", "F. FAQ"):
        assert output.count(option) == 5


@pytest.mark.parametrize("default, answer, expected", [
    (False, "", False), (True, "", True), (False, "1", True), (True, "2", False),
])
def test_confirmation_utilities_do_not_confirm(enter, capsys, default, answer, expected):
    backup = Mock()
    reader = enter("B", "F", answer)
    with prompts.menu_utilities(backup):
        assert prompts.confirm("Really proceed?", default) is expected
    backup.assert_called_once_with()
    assert reader.call_count == 3
    output = capsys.readouterr().out
    assert output.count("Really proceed?") == 3
    assert output.count("1. Yes") == output.count("2. No") == 3


def test_invalid_numeric_choices_still_require_valid_selection(enter, capsys):
    reader = enter("0", "4", "invalid", "3")
    assert prompts.menu("Selection", ["One", "Two", "Back"]) == 3
    assert reader.call_count == 4
    assert capsys.readouterr().out.count("Enter a number from 1 to 3") == 3


@pytest.mark.parametrize("value", ["b", "f", "B", "F"])
def test_text_shortcuts_are_literal(enter, monkeypatch, value):
    backup, faq = Mock(), Mock()
    monkeypatch.setattr(prompts, "show_faq", faq)
    reader = enter(value)
    with prompts.menu_utilities(backup):
        assert prompts.text("Title", "Old title", required=True) == value
    backup.assert_not_called()
    faq.assert_not_called()
    reader.assert_called_once()


@pytest.mark.parametrize("fails", [False, True])
def test_nested_show_form_keeps_pending_mutable_edits(show, enter, capsys, fails):
    original = deepcopy(show)
    backup = Mock(side_effect=OSError("backup unavailable") if fails else None)
    reader = enter(
        "2", "1", "Pending title",
        "2", str(prompts.SHOW_FIELDS.index("podroll") + 1),
        "2", "1", "", "", "Pending entry", "B", "F", "", "",
    )
    with prompts.menu_utilities(backup):
        result = prompts.show_form(show)
    expected = deepcopy(original)
    expected["title"] = "Pending title"
    expected["podroll"][0]["title"] = "Pending entry"
    assert result == expected
    assert show == original
    assert result["podroll"] is not show["podroll"]
    backup.assert_called_once_with()
    assert reader.call_count == 14
    output = capsys.readouterr().out
    assert "Termicast FAQ" in output
    assert output.count("4. Done") == 4
    assert ("Menu action failed: backup unavailable" in output) is fails


@pytest.mark.parametrize("exception", [RuntimeError, EOFError, KeyboardInterrupt])
def test_nested_context_restores_handlers_after_exception(enter, capsys, exception):
    outer, inner = Mock(), Mock()
    reader = enter("B", "1", "B", "1", "B", "1")
    with prompts.menu_utilities(outer):
        with pytest.raises(exception):
            with prompts.menu_utilities(inner):
                assert prompts.menu("Inner", ["Back"]) == 1
                raise exception("leave context")
        assert prompts._backup_action.get() is outer
        assert prompts.menu("Outer", ["Back"]) == 1
    assert prompts._backup_action.get() is None
    assert prompts.menu("Outside", ["Back"]) == 1
    inner.assert_called_once_with()
    outer.assert_called_once_with()
    assert reader.call_count == 6
    assert "Backup is available within a Termicast session" in capsys.readouterr().out


@pytest.mark.parametrize("exception, status, message", [
    (EOFError, 0, "Input closed. Exiting."),
    (KeyboardInterrupt, 130, "Cancelled. Exiting."),
    (RuntimeError, 1, "input failed"),
])
def test_main_resets_backup_handler_on_input_exception(enter, capsys, exception, status, message):
    answers = [exception("input failed")] if exception is RuntimeError else ["B", exception("input failed")]
    reader = enter(*answers)
    assert cli.main([]) == status
    assert reader.call_count == len(answers)
    assert prompts._backup_action.get() is None
    assert message in capsys.readouterr().out
    enter("B", "1")
    assert prompts.menu("After session", ["Back"]) == 1
    assert "Backup is available within a Termicast session" in capsys.readouterr().out


@pytest.mark.parametrize("custom_destination", [False, True])
def test_backup_main_creates_real_archive(tmp_path, isolated_state, show, capsys, custom_destination):
    selected = tmp_path / "selected-state"
    db = Database(selected / "termicast.db")
    db.save_show(show)
    Publisher(db).regenerate(show["id"])
    feed = Path(show["output_dir"]) / "feed.xml"
    destination = tmp_path / "private-archives" if custom_destination else selected / "backups"
    args = ["--data-dir", str(selected), "backup"]
    if custom_destination:
        args.append(str(destination))
    assert cli.main(args) == 0
    archive_path, = destination.glob("*.zip")
    with ZipFile(archive_path) as archive:
        assert archive.testzip() is None
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["source_database"] == str(db.path)
        assert manifest["missing_files"] == []
        assert [record["id"] for record in manifest["shows"]] == [show["id"]]
        assert archive.read(f"outputs/{show['id']}/feed.xml") == feed.read_bytes()
        archive.extract("termicast.db", tmp_path / "snapshot")
    assert Database(tmp_path / "snapshot" / "termicast.db").get_show(show["id"]) == show
    assert "Backup saved:" in capsys.readouterr().out
    assert not isolated_state.exists()
    if custom_destination:
        assert not (selected / "backups").exists()


@pytest.mark.parametrize("explicit_data_dir", [False, True])
def test_faq_main_does_not_open_or_create_database(tmp_path, isolated_state, monkeypatch, capsys,
                                                  explicit_data_dir):
    database = Mock(side_effect=AssertionError("FAQ must not open a database"))
    monkeypatch.setattr(cli, "Database", database)
    selected = tmp_path / "faq-state"
    args = ["--data-dir", str(selected)] if explicit_data_dir else []
    assert cli.main(args + ["faq"]) == 0
    database.assert_not_called()
    assert not isolated_state.exists()
    assert not selected.exists()
    output = capsys.readouterr().out
    assert "Termicast FAQ" in output
    assert "How do I restore a backup?" in output


def test_interactive_main_backup_faq_quit(enter, isolated_state, capsys):
    reader = enter("B", "", "F", "5")
    assert cli.main([]) == 0
    assert reader.call_count == 4
    archive_path, = (isolated_state / "backups").glob("*.zip")
    with ZipFile(archive_path) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == {"termicast.db", "manifest.json"}
        assert json.loads(archive.read("manifest.json"))["shows"] == []
    output = capsys.readouterr().out
    assert output.count("5. Quit") == 3
    assert output.index("Backup saved:") < output.index("Termicast FAQ")
    assert "Input closed" not in output
    assert "failed" not in output.lower()
    assert prompts._backup_action.get() is None


def test_podcast_nested_backup_preserves_unsaved_form_without_publication(
    show, tmp_path, isolated_state, enter, monkeypatch, capsys,
):
    db = Database()
    db.save_show(show)
    publisher = Mock(spec=Publisher)
    monkeypatch.setattr(cli, "Publisher", Mock(return_value=publisher))
    review = Mock(wraps=prompts.review)
    monkeypatch.setattr(prompts, "review", review)
    reader = enter(
        "1", "1", "3", "2", "1", "Pending title",
        "2", str(prompts.SHOW_FIELDS.index("podroll") + 1),
        "2", "1", "", "", "Pending entry", "B", "", "F", "",
        "3", "5", "5",
    )
    assert cli.main([]) == 0
    assert reader.call_count == 20
    settings = [call.args[1] for call in review.call_args_list if call.args[0] == "Podcast settings"]
    expected = deepcopy(show)
    expected["title"] = "Pending title"
    expected["podroll"][0]["title"] = "Pending entry"
    assert settings[-1] == expected
    assert db.get_show(show["id"]) == show
    assert db.list_episodes(show["id"]) == []
    assert publisher.mock_calls == []
    assert not Path(show["output_dir"]).exists()
    archive_path, = (isolated_state / "backups").glob("*.zip")
    with ZipFile(archive_path) as archive:
        archive.extract("termicast.db", tmp_path / "snapshot")
    with sqlite3.connect(tmp_path / "snapshot" / "termicast.db") as snapshot:
        saved, = snapshot.execute("SELECT settings FROM shows").fetchall()
        assert json.loads(saved[0]) == show
        assert snapshot.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0
    output = capsys.readouterr().out
    assert "Backup saved:" in output
    assert "Termicast FAQ" in output
    assert "Input closed" not in output
    assert "failed" not in output.lower()
    assert prompts._backup_action.get() is None
