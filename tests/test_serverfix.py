from unittest.mock import Mock

import pytest

from termicast import serverfix


def test_detection_uses_control_tools(monkeypatch):
    monkeypatch.setattr(serverfix.shutil, "which", lambda name: "/tools/" + name)
    assert serverfix.detect_servers() == {"Nginx": "/tools/nginx", "Apache": "/tools/apache2ctl"}


def prepare(monkeypatch, tmp_path):
    config = tmp_path / "site.conf"
    config.write_text("original")
    monkeypatch.setenv("EDITOR", "editor --wait")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(serverfix.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(serverfix.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path / "backups"))
    (tmp_path / "backups").mkdir()
    def edit(args):
        assert args == ["editor", "--wait", str(config)]
        config.write_text("corrected")
        return Mock(returncode=0)
    monkeypatch.setattr(serverfix.subprocess, "run", edit)
    return config


def test_edit_validates_and_retains_backup_without_reload(monkeypatch, tmp_path):
    config = prepare(monkeypatch, tmp_path)
    control = Mock()
    monkeypatch.setattr(serverfix, "run_control", control)
    backup, changed = serverfix.edit_site_config(config, "/bin/nginx")
    assert changed
    assert backup.read_text() == "original"
    assert backup.stat().st_mode & 0o777 == 0o600
    assert config.read_text() == "corrected"
    assert control.call_args_list == [(("/bin/nginx", "-t"),), (("/bin/nginx", "-t"),)]


def test_invalid_edit_restores_original(monkeypatch, tmp_path):
    config = prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(serverfix, "run_control", Mock(side_effect=[None, RuntimeError("invalid syntax")]))
    with pytest.raises(RuntimeError, match="Configuration restored"):
        serverfix.edit_site_config(config, "/bin/nginx")
    assert config.read_text() == "original"


def test_existing_invalid_config_does_not_open_editor(monkeypatch, tmp_path):
    config = prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(serverfix, "run_control", Mock(side_effect=RuntimeError("existing error")))
    with pytest.raises(RuntimeError, match="existing error"):
        serverfix.edit_site_config(config, "/bin/nginx")
    assert config.read_text() == "original"
    assert list((tmp_path / "backups").iterdir()) == []


@pytest.mark.parametrize("name,args", [("Nginx", ("-s", "reload")), ("Apache", ("-k", "graceful"))])
def test_reload_checks_syntax_first(monkeypatch, name, args):
    control = Mock()
    monkeypatch.setattr(serverfix, "run_control", control)
    serverfix.reload_server(name, "/tool")
    assert control.call_args_list == [(("/tool", "-t"),), (("/tool", *args),)]


@pytest.mark.parametrize("action", [2, 4])
def test_hosting_offers_correction_after_mime_failure(monkeypatch, action):
    from termicast import hosting, prompts
    choices = iter([action, 9])
    monkeypatch.setattr(prompts, "menu", lambda *args, **kwargs: next(choices))
    monkeypatch.setattr(prompts, "confirm", lambda *args: True)
    repair = Mock()
    monkeypatch.setattr(prompts, "correct_host_mime", repair)
    problem = "Unexpected Content-Type 'text/xml' (expected application/rss+xml): https://e.org/feed.xml"
    monkeypatch.setattr(hosting, "doctor", lambda *args: [problem])
    publisher = Mock()
    publisher.deploy.side_effect = RuntimeError(problem)
    db = Mock()
    show = {"id": "001"}
    prompts.hosting_menu(db, publisher, show)
    repair.assert_called_once_with(db, show)


def test_no_detected_server_does_not_offer_edit(monkeypatch):
    from termicast import prompts
    monkeypatch.setattr(serverfix, "detect_servers", lambda: {})
    edit = Mock()
    monkeypatch.setattr(serverfix, "edit_site_config", edit)
    prompts.correct_host_mime(Mock(), {})
    edit.assert_not_called()


def test_hosting_menu_validates_s3_access_before_save(monkeypatch):
    from termicast import prompts, s3deploy
    choices = iter([1, 9])
    monkeypatch.setattr(prompts, "menu", lambda *args, **kwargs: next(choices))
    monkeypatch.setattr(prompts, "hosting_form", lambda d: d.__setitem__("hosting", "s3"))
    checks = []
    monkeypatch.setattr(s3deploy, "check_s3_access", lambda s: checks.append(s) or None)
    db = Mock()
    publisher = Mock()
    show = {"id": "001"}
    prompts.hosting_menu(db, publisher, show)
    assert len(checks) == 1
    db.save_show.assert_called_once()


def test_hosting_menu_retains_settings_when_s3_access_fails(monkeypatch):
    from termicast import prompts, s3deploy
    choices = iter([1, 9])
    monkeypatch.setattr(prompts, "menu", lambda *args, **kwargs: next(choices))
    monkeypatch.setattr(prompts, "hosting_form", lambda d: d.__setitem__("hosting", "s3"))
    monkeypatch.setattr(s3deploy, "check_s3_access",
                        lambda s: (_ for _ in ()).throw(RuntimeError("no credentials")))
    db = Mock()
    publisher = Mock()
    show = {"id": "001"}
    prompts.hosting_menu(db, publisher, show)
    db.save_show.assert_not_called()
