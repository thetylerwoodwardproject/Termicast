import os

import pytest

from termicast import cli
from termicast.models import new_show


def test_help_lists_new_commands(data_dir, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    for command in ("add", "deploy", "doctor", "publish-due", "validate", "archive", "restage"):
        assert command in output
    assert not data_dir.exists()


def test_disable_unsupported_cpr_probe_sets_default(monkeypatch):
    monkeypatch.delenv("PROMPT_TOOLKIT_NO_CPR", raising=False)
    cli._disable_unsupported_cpr_probe()
    assert os.environ["PROMPT_TOOLKIT_NO_CPR"] == "1"


def test_disable_unsupported_cpr_probe_respects_explicit_opt_out(monkeypatch):
    monkeypatch.setenv("PROMPT_TOOLKIT_NO_CPR", "0")
    cli._disable_unsupported_cpr_probe()
    assert os.environ["PROMPT_TOOLKIT_NO_CPR"] == "0"


def test_validate_empty(capsys):
    assert cli.main(["validate"]) == 0
    assert "No managed feeds to validate." in capsys.readouterr().out


def test_publish_due_empty(data_dir, capsys):
    assert cli.main(["publish-due"]) == 0
    assert "Published 0 due episode(s)." in capsys.readouterr().out


def test_doctor_empty(capsys):
    assert cli.main(["doctor"]) == 0
    assert "No hosting problems found." in capsys.readouterr().out


def test_doctor_unknown_show(capsys):
    assert cli.main(["doctor", "missing"]) == 1
    assert "Unknown podcast ID" in capsys.readouterr().out


def test_deploy_unknown_show(capsys):
    assert cli.main(["deploy", "missing"]) == 1
    assert "Unknown podcast ID" in capsys.readouterr().out


def test_add_unknown_show(capsys):
    assert cli.main(["add", "missing", "audio.mp3"]) == 1
    assert "Unknown podcast ID" in capsys.readouterr().out


# --- the import wizard's output-directory and hosting prompts -------------

def test_pick_output_dir_without_an_existing_feed(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "edit_field",
                        lambda data, field: data.__setitem__(field, str(tmp_path)))
    assert cli._pick_output_dir({}) is False


def test_pick_output_dir_overwrite(monkeypatch, tmp_path):
    (tmp_path / "feed.xml").write_text("old")
    monkeypatch.setattr(cli, "edit_field",
                        lambda data, field: data.__setitem__(field, str(tmp_path)))
    monkeypatch.setattr(cli, "menu", lambda *a, **k: 1)
    assert cli._pick_output_dir({}) is True


def test_pick_output_dir_cancel(monkeypatch, tmp_path):
    from termicast.prompts import Cancelled
    (tmp_path / "feed.xml").write_text("old")
    monkeypatch.setattr(cli, "edit_field",
                        lambda data, field: data.__setitem__(field, str(tmp_path)))
    monkeypatch.setattr(cli, "menu", lambda *a, **k: 3)
    with pytest.raises(Cancelled):
        cli._pick_output_dir({})


def test_pick_output_dir_choose_a_different_directory(monkeypatch, tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "feed.xml").write_text("old")
    fresh = tmp_path / "fresh"
    picks = iter([str(existing), str(fresh)])
    destination = {}
    monkeypatch.setattr(cli, "edit_field",
                        lambda data, field: data.__setitem__(field, next(picks)))
    monkeypatch.setattr(cli, "menu", lambda *a, **k: 2)
    assert cli._pick_output_dir(destination) is False
    assert destination["output_dir"] == str(fresh)


def test_configure_hosting_skips_the_s3_check_for_local(monkeypatch):
    from termicast import prompts
    monkeypatch.setattr(prompts, "hosting_form", lambda data: data.__setitem__("hosting", "local"))
    destination = {}
    cli._configure_hosting(destination)
    assert destination["hosting"] == "local"


def test_configure_hosting_verifies_s3_before_continuing(monkeypatch):
    from termicast import prompts, s3deploy
    monkeypatch.setattr(prompts, "hosting_form", lambda data: data.__setitem__("hosting", "s3"))
    calls = []
    monkeypatch.setattr(s3deploy, "check_s3_destination", lambda show: calls.append(show))
    cli._configure_hosting({})
    assert len(calls) == 1


def test_configure_hosting_declines_retry_and_cancels(monkeypatch):
    from termicast import prompts, s3deploy
    from termicast.prompts import Cancelled

    def fail(show):
        raise RuntimeError("no such bucket")

    monkeypatch.setattr(prompts, "hosting_form", lambda data: data.__setitem__("hosting", "s3"))
    monkeypatch.setattr(s3deploy, "check_s3_destination", fail)
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: False)
    with pytest.raises(Cancelled):
        cli._configure_hosting({})


def test_load_resume_returns_none_without_a_checkpoint(db):
    assert cli._load_resume(db) is None


def test_save_and_load_resume_round_trip(db):
    destination = new_show(output_dir="/srv/show", hosting="s3", bucket="b")
    cli._save_resume(db, destination, True)
    loaded_destination, loaded_overwrite = cli._load_resume(db)
    assert loaded_destination == destination
    assert loaded_overwrite is True


def test_clear_resume_is_a_noop_without_a_checkpoint(db):
    cli._clear_resume(db)


def test_load_resume_ignores_a_corrupt_checkpoint(db):
    path = cli._resume_path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json")
    assert cli._load_resume(db) is None


def test_create_or_import_resumes_saved_setup_and_skips_pickup(monkeypatch, db):
    saved = new_show(output_dir="/srv/show", base_url="https://example.org/show", hosting="local")
    cli._save_resume(db, saved, False)
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: True)

    def fail_pick(destination):
        raise AssertionError("should not re-prompt for output_dir when resuming")
    monkeypatch.setattr(cli, "_pick_output_dir", fail_pick)

    class Stop(Exception):
        pass

    monkeypatch.setattr(cli, "menu", lambda *a, **k: (_ for _ in ()).throw(Stop()))
    with pytest.raises(Stop):
        cli._create_or_import(db, object(), importing=True)


def test_create_or_import_declines_resume_clears_checkpoint_and_starts_fresh(monkeypatch, db):
    saved = new_show(output_dir="/srv/show", base_url="https://example.org/show", hosting="local")
    cli._save_resume(db, saved, False)
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: False)

    class Stop(Exception):
        pass

    def stop_pick(destination):
        raise Stop()
    monkeypatch.setattr(cli, "_pick_output_dir", stop_pick)
    with pytest.raises(Stop):
        cli._create_or_import(db, object(), importing=True)
    assert cli._load_resume(db) is None
