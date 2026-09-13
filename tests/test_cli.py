import json
import os
from unittest.mock import Mock

import pytest

from termicast import cli, s3deploy, storage
from termicast.models import new_show
from termicast.storage import asset_root


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
    cli._save_resume(db, destination, True, "hosting")
    loaded_destination, loaded_overwrite, loaded_progress = cli._load_resume(db)
    assert loaded_destination == destination
    assert loaded_overwrite is True
    assert loaded_progress == "hosting"


def test_clear_resume_is_a_noop_without_a_checkpoint(db):
    cli._clear_resume(db)


def test_load_resume_ignores_a_corrupt_checkpoint(db):
    path = cli._resume_path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json")
    assert cli._load_resume(db) is None


def test_load_resume_ignores_malformed_envelopes(db):
    path = cli._resume_path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    malformed = (
        "[]",
        "{}",
        '{"destination": "not-a-dict", "overwrite": true}',
        '{"destination": {}, "overwrite": "yes"}',
        '{"destination": {}, "overwrite": true, "progress": "bogus"}',
    )
    for raw in malformed:
        path.write_text(raw)
        assert cli._load_resume(db) is None


def test_create_or_import_resumes_saved_setup_and_skips_pickup(monkeypatch, db):
    saved = new_show(output_dir="/srv/show", base_url="https://example.org/show", hosting="local")
    cli._save_resume(db, saved, False, "hosting")
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: True)

    def fail_pick(destination):
        raise AssertionError("should not re-prompt for output_dir when resuming")
    monkeypatch.setattr(cli, "_pick_output_dir", fail_pick)

    def fail_hosting(destination):
        raise AssertionError("should not re-configure hosting when resuming")
    monkeypatch.setattr(cli, "_configure_hosting", fail_hosting)

    class Stop(Exception):
        pass

    monkeypatch.setattr(cli, "menu", lambda *a, **k: (_ for _ in ()).throw(Stop()))
    with pytest.raises(Stop):
        cli._create_or_import(db, object(), importing=True)


def test_create_or_import_declines_resume_clears_checkpoint_and_starts_fresh(monkeypatch, db):
    saved = new_show(output_dir="/srv/show", base_url="https://example.org/show", hosting="local")
    cli._save_resume(db, saved, False, "hosting")
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: False)

    class Stop(Exception):
        pass

    def stop_pick(destination):
        raise Stop()
    monkeypatch.setattr(cli, "_pick_output_dir", stop_pick)
    with pytest.raises(Stop):
        cli._create_or_import(db, object(), importing=True)
    assert cli._load_resume(db) is None


def test_resume_after_output_selection_reasks_base_url_and_hosting(monkeypatch, db):
    saved = new_show(output_dir="/srv/show")
    cli._save_resume(db, saved, False, "output")
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: True)
    steps = []
    monkeypatch.setattr(cli, "edit_field", lambda data, field: steps.append(field))
    monkeypatch.setattr(cli, "_configure_hosting", lambda data: steps.append("hosting"))

    class Stop(Exception):
        pass
    monkeypatch.setattr(cli, "menu", lambda *a, **k: (_ for _ in ()).throw(Stop()))
    with pytest.raises(Stop):
        cli._create_or_import(db, object(), importing=True)
    assert "base_url" in steps
    assert "hosting" in steps


def test_resume_after_base_url_only_reasks_hosting(monkeypatch, db):
    saved = new_show(output_dir="/srv/show", base_url="https://example.org/show")
    cli._save_resume(db, saved, False, "base_url")
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: True)
    steps = []
    monkeypatch.setattr(cli, "edit_field", lambda data, field: steps.append(field))
    monkeypatch.setattr(cli, "_configure_hosting", lambda data: steps.append("hosting"))

    class Stop(Exception):
        pass
    monkeypatch.setattr(cli, "menu", lambda *a, **k: (_ for _ in ()).throw(Stop()))
    with pytest.raises(Stop):
        cli._create_or_import(db, object(), importing=True)
    assert steps == ["hosting"]


def test_resume_legacy_checkpoint_reasks_base_url_and_hosting(monkeypatch, db):
    saved = new_show(output_dir="/srv/show", base_url="https://example.org/show", hosting="local")
    path = cli._resume_path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"destination": saved, "overwrite": False}))
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: True)
    steps = []
    monkeypatch.setattr(cli, "edit_field", lambda data, field: steps.append(field))
    monkeypatch.setattr(cli, "_configure_hosting", lambda data: steps.append("hosting"))

    def fail_pick(destination):
        raise AssertionError("should not re-prompt for output_dir for a legacy checkpoint")
    monkeypatch.setattr(cli, "_pick_output_dir", fail_pick)

    class Stop(Exception):
        pass
    monkeypatch.setattr(cli, "menu", lambda *a, **k: (_ for _ in ()).throw(Stop()))
    with pytest.raises(Stop):
        cli._create_or_import(db, object(), importing=True)
    assert steps == ["base_url", "hosting"]


def test_failed_s3_check_keeps_checkpoint_at_base_url(monkeypatch, db):
    from termicast.prompts import Cancelled
    saved = new_show(output_dir="/srv/show", base_url="https://example.org/show")
    cli._save_resume(db, saved, False, "base_url")
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: True)

    def fail_hosting(data):
        raise Cancelled()
    monkeypatch.setattr(cli, "_configure_hosting", fail_hosting)
    with pytest.raises(Cancelled):
        cli._create_or_import(db, object(), importing=True)
    _, _, progress = cli._load_resume(db)
    assert progress == "base_url"


def test_finish_import_deploys_s3_when_auto_deploy_is_off(db):
    cli._save_resume(db, new_show(output_dir="/srv/show"), False, "hosting")
    show = new_show(id="001", hosting="s3", bucket="b", enabled=False)
    publisher = Mock()
    cli._finish_import(publisher, db, show, "")
    publisher.deploy.assert_called_once_with("001")
    assert cli._load_resume(db) is None


def test_finish_import_skips_deploy_when_auto_deploy_already_ran(db):
    show = new_show(id="001", hosting="s3", bucket="b", enabled=True)
    publisher = Mock()
    cli._finish_import(publisher, db, show, "")
    publisher.deploy.assert_not_called()


def test_finish_import_skips_deploy_for_local_hosting(db):
    show = new_show(id="001", hosting="local")
    publisher = Mock()
    cli._finish_import(publisher, db, show, "")
    publisher.deploy.assert_not_called()


def test_finish_import_failure_does_not_print_success(db, capsys):
    cli._save_resume(db, new_show(output_dir="/srv/show"), False, "hosting")
    show = new_show(id="001", hosting="s3", bucket="b", enabled=False)
    publisher = Mock()
    publisher.deploy.side_effect = RuntimeError("boom")
    cli._finish_import(publisher, db, show, "")
    out = capsys.readouterr().out
    assert "Podcast saved and feed generated." not in out
    assert "termicast deploy 001" in out
    assert cli._load_resume(db) is None


def test_finish_import_prints_success_after_upload(db, capsys):
    cli._save_resume(db, new_show(output_dir="/srv/show"), False, "hosting")
    show = new_show(id="001", hosting="s3", bucket="b", enabled=False)
    publisher = Mock()
    cli._finish_import(publisher, db, show, "")
    out = capsys.readouterr().out
    assert "Podcast saved and feed generated." in out


def test_hosting_banner_prints_before_regeneration(monkeypatch, db, capsys):
    show = new_show(
        title="S", description="d", base_url="https://example.org/show",
        output_dir="/tmp/termicast-banner", hosting="s3", bucket="b", prefix="p",
        asset_base_url="https://cdn.example.org/show", enabled=True, mirror_feed=True,
    )
    monkeypatch.setattr(cli, "show_form", lambda *a, **k: show)
    calls = []
    publisher = Mock()

    def regenerate(show_id):
        calls.append("regenerate")
        raise RuntimeError("boom")
    publisher.regenerate = regenerate
    with pytest.raises(RuntimeError):
        cli._create_or_import(db, publisher, importing=False)
    out = capsys.readouterr().out
    assert "Hosting: S3-compatible storage." in out
    assert "Mirror feed.xml to S3: on" in out
    assert calls == ["regenerate"]


# --- "Delete podcast" ------------------------------------------------------

def test_interactive_menu_offers_delete_podcast(monkeypatch, db):
    from termicast import prompts
    from termicast.prompts import Cancelled

    def fake_menu(title, options, default=None, headers=None):
        assert "Delete podcast" in options
        raise Cancelled()
    # The loop is driven by prompts.run_menu, so that is where menu resolves.
    monkeypatch.setattr(prompts, "menu", fake_menu)
    with pytest.raises(Cancelled):
        cli._interactive(db, Mock())


def test_delete_show_cancels_on_id_mismatch(db, show, monkeypatch, capsys):
    monkeypatch.setattr(cli, "text", lambda *a, **k: "not-the-id")
    confirmed = []
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: confirmed.append(1) or True)
    cli._delete_show(db, show)
    assert not confirmed
    assert db.get_show(show["id"]) is not None
    assert "Cancelled. Nothing was changed." in capsys.readouterr().out


def test_delete_show_cancels_on_final_no(db, show, monkeypatch):
    monkeypatch.setattr(cli, "text", lambda *a, **k: show["id"])
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: False)
    cli._delete_show(db, show)
    assert db.get_show(show["id"]) is not None


def test_delete_show_removes_local_files_and_db_row(db, show, monkeypatch):
    root = asset_root(show)
    (root / "audio").mkdir(parents=True)
    (root / "audio" / "ep1.mp3").write_bytes(b"data")
    monkeypatch.setattr(cli, "text", lambda *a, **k: show["id"])
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: True)

    cli._delete_show(db, show)

    assert not root.exists()
    assert db.get_show(show["id"]) is None


def test_delete_show_deletes_s3_prefix_before_local_and_db(db, monkeypatch, tmp_path):
    record = new_show(
        title="S3 show", description="d", author="a", owner_name="o",
        owner_email="o@example.org", website="https://example.org", category="Technology",
        base_url="https://example.org", output_dir=str(tmp_path / "out"), timezone="UTC",
        explicit=False, hosting="s3", bucket="my-bucket", prefix="my-show",
        asset_base_url="https://cdn.example.org",
    )
    s3_show = db.save_show(record)
    (tmp_path / "out").mkdir(parents=True)

    calls = []
    monkeypatch.setattr(cli, "text", lambda *a, **k: s3_show["id"])
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(s3deploy, "delete_prefix",
                        lambda show, dry_run=False, allow_empty_prefix=False: calls.append(("s3", dry_run)) or 0)
    monkeypatch.setattr(storage, "delete_local_assets",
                        lambda show, dry_run=False: calls.append(("local", dry_run)) or [])

    cli._delete_show(db, s3_show)

    assert calls == [("s3", True), ("s3", False), ("local", False)]
    assert db.get_show(s3_show["id"]) is None


def test_delete_show_does_not_touch_local_or_db_when_s3_preflight_fails(db, monkeypatch, tmp_path):
    record = new_show(
        title="S3 show", description="d", author="a", owner_name="o",
        owner_email="o@example.org", website="https://example.org", category="Technology",
        base_url="https://example.org", output_dir=str(tmp_path / "out"), timezone="UTC",
        explicit=False, hosting="s3", bucket="my-bucket", prefix="my-show",
        asset_base_url="https://cdn.example.org",
    )
    s3_show = db.save_show(record)
    (tmp_path / "out").mkdir(parents=True)

    def fail_preflight(show, dry_run=False, allow_empty_prefix=False):
        raise RuntimeError("S3 credentials are not configured")
    monkeypatch.setattr(s3deploy, "delete_prefix", fail_preflight)
    local_delete = Mock()
    monkeypatch.setattr(storage, "delete_local_assets", local_delete)
    text_calls = []
    monkeypatch.setattr(cli, "text", lambda *a, **k: text_calls.append(1) or s3_show["id"])

    with pytest.raises(RuntimeError, match="S3 credentials are not configured"):
        cli._delete_show(db, s3_show)

    assert not text_calls
    local_delete.assert_not_called()
    assert db.get_show(s3_show["id"]) is not None


def _bucket_root_show(db, tmp_path):
    record = new_show(
        title="Bucket Root Show", description="d", author="a", owner_name="o",
        owner_email="o@example.org", website="https://example.org", category="Technology",
        base_url="https://example.org", output_dir=str(tmp_path / "out"), timezone="UTC",
        explicit=False, hosting="s3", bucket="my-bucket", prefix="",
        asset_base_url="https://cdn.example.org",
    )
    show = db.save_show(record)
    (tmp_path / "out").mkdir(parents=True)
    return show


def test_delete_show_bucket_root_cancelled_at_initial_warning(db, monkeypatch, tmp_path):
    show = _bucket_root_show(db, tmp_path)
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: False)
    delete_prefix_mock = Mock()
    monkeypatch.setattr(s3deploy, "delete_prefix", delete_prefix_mock)

    cli._delete_show(db, show)

    delete_prefix_mock.assert_not_called()
    assert db.get_show(show["id"]) is not None


def test_delete_show_bucket_root_cancelled_on_wrong_bucket_name(db, monkeypatch, tmp_path):
    show = _bucket_root_show(db, tmp_path)
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli, "text", lambda *a, **k: "wrong-bucket")
    delete_prefix_mock = Mock()
    monkeypatch.setattr(s3deploy, "delete_prefix", delete_prefix_mock)

    cli._delete_show(db, show)

    delete_prefix_mock.assert_not_called()
    assert db.get_show(show["id"]) is not None


def test_delete_show_bucket_root_deletes_when_fully_confirmed(db, monkeypatch, tmp_path):
    show = _bucket_root_show(db, tmp_path)
    confirms = iter([True, True])
    texts = iter(["my-bucket", show["id"]])
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: next(confirms))
    monkeypatch.setattr(cli, "text", lambda *a, **k: next(texts))
    calls = []
    monkeypatch.setattr(
        s3deploy, "delete_prefix",
        lambda show, dry_run=False, allow_empty_prefix=False: calls.append((dry_run, allow_empty_prefix)) or 0)
    monkeypatch.setattr(storage, "delete_local_assets", lambda show, dry_run=False: [])

    cli._delete_show(db, show)

    assert calls == [(True, True), (False, True)]
    assert db.get_show(show["id"]) is None


def test_scriptable_commands_do_not_load_the_interactive_stack():
    """`termicast publish-due` runs from cron and never draws a menu.

    questionary/prompt_toolkit cost ~150 ms to import and rich.markdown
    another ~45 ms. Importing them eagerly made every scriptable invocation
    pay for machinery it never touches, so this pins the deferral.
    """
    import subprocess
    import sys

    probe = (
        "import sys, termicast.cli;"
        "print(','.join(m for m in "
        "('questionary','prompt_toolkit','rich.markdown','lxml.etree','PIL') "
        "if m in sys.modules))"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", f"eagerly imported: {result.stdout.strip()}"
