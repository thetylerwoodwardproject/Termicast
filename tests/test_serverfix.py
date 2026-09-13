import os
import subprocess
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


def test_run_control_uses_sudo_when_not_root(monkeypatch):
    monkeypatch.setattr(serverfix.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(serverfix.shutil, "which", lambda name: "/usr/bin/" + name)
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(serverfix.subprocess, "run", run)
    serverfix.run_control("/usr/sbin/nginx", "-t")
    assert run.call_args.args[0] == ["/usr/bin/sudo", "/usr/sbin/nginx", "-t"]


def test_run_control_skips_sudo_when_root(monkeypatch):
    monkeypatch.setattr(serverfix.os, "geteuid", lambda: 0)
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(serverfix.subprocess, "run", run)
    serverfix.run_control("/usr/sbin/nginx", "-t")
    assert run.call_args.args[0] == ["/usr/sbin/nginx", "-t"]


def test_editor_uses_sudo_when_config_not_writable(monkeypatch, tmp_path):
    config = tmp_path / "site.conf"
    config.write_text("original")
    monkeypatch.setenv("EDITOR", "vim")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(serverfix.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(serverfix.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(serverfix.os, "access", lambda path, mode: mode != os.W_OK)
    monkeypatch.setattr(serverfix, "run_control", Mock(return_value=None))
    monkeypatch.setattr(serverfix.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path / "backups"))
    (tmp_path / "backups").mkdir()
    edits = []

    def fake_run(args, **kwargs):
        edits.append(args)
        return Mock(returncode=0)
    monkeypatch.setattr(serverfix.subprocess, "run", fake_run)
    serverfix.edit_site_config(config, "/usr/sbin/nginx")
    assert edits and edits[0] == ["/usr/bin/sudo", "vim", str(config)]


def test_write_bytes_uses_sudo_when_not_writable(monkeypatch, tmp_path):
    target = tmp_path / "site.conf"
    target.write_text("original")
    monkeypatch.setattr(serverfix.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(serverfix.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(serverfix.os, "access", lambda path, mode: False)
    cp_calls = []

    def fake_run(args, **kwargs):
        cp_calls.append(args)
        return subprocess.CompletedProcess([], 0, "", "")
    monkeypatch.setattr(serverfix.subprocess, "run", fake_run)
    serverfix._write_bytes(target, b"new")
    assert cp_calls and cp_calls[0][0:2] == ["/usr/bin/sudo", "cp"]
    assert cp_calls[0][3] == str(target)


def _backups(tmp_path, monkeypatch):
    monkeypatch.setattr(serverfix.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path / "backups"))
    (tmp_path / "backups").mkdir()


def test_mime_snippet_content():
    assert "application/rss+xml" in serverfix.mime_snippet("Nginx")
    assert "image/png" in serverfix.mime_snippet("Nginx")
    assert "image/webp" in serverfix.mime_snippet("Nginx")
    assert "AddType application/json+chapters" in serverfix.mime_snippet("Apache")
    assert "AddType image/png" in serverfix.mime_snippet("Apache")


def test_insert_block_after_server_name():
    config = (
        "server {\n"
        "    listen 80;\n"
        "    server_name media.example.me;\n"
        "    root /var/www/media;\n"
        "}\n"
    )
    result = serverfix._insert_block("Nginx", config)
    assert result is not None
    joined = "\n".join(result)
    assert serverfix.MIME_MARKER in joined
    assert joined.index("server_name") < joined.index("types {") < joined.index("root")


def test_insert_block_missing_anchor_returns_none():
    assert serverfix._insert_block("Nginx", "http {\n    include mime.types;\n}\n") is None


def test_apply_mime_patch_inserts_and_validates(tmp_path, monkeypatch):
    _backups(tmp_path, monkeypatch)
    config = tmp_path / "site.conf"
    config.write_text("server {\n    server_name media.example.me;\n}\n")
    control = Mock()
    monkeypatch.setattr(serverfix, "run_control", control)
    backup, changed = serverfix.apply_mime_patch("Nginx", config, "/usr/sbin/nginx")
    assert changed is True
    assert serverfix.MIME_MARKER in config.read_text()
    control.assert_called_once_with("/usr/sbin/nginx", "-t")


def test_apply_mime_patch_is_idempotent(tmp_path, monkeypatch):
    _backups(tmp_path, monkeypatch)
    config = tmp_path / "site.conf"
    block = serverfix.mime_snippet("Nginx").rstrip("\n")
    config.write_text("server {\n    server_name media.example.me;\n" + block + "\n}\n")
    monkeypatch.setattr(serverfix, "run_control", Mock())
    backup, changed = serverfix.apply_mime_patch("Nginx", config, "/usr/sbin/nginx")
    assert changed is False
    assert backup is None


def test_apply_mime_patch_upgrades_outdated_block(tmp_path, monkeypatch):
    _backups(tmp_path, monkeypatch)
    config = tmp_path / "site.conf"
    old_block = (
        "# Termicast podcast MIME types\n"
        "types {\n"
        "    application/rss+xml       xml;\n"
        "    application/json+chapters json;\n"
        "    image/jpeg                jpg jpeg;\n"
        "}\n"
    )
    config.write_text("server {\n    server_name media.example.me;\n" + old_block + "}\n")
    monkeypatch.setattr(serverfix, "run_control", Mock())
    backup, changed = serverfix.apply_mime_patch("Nginx", config, "/usr/sbin/nginx")
    assert changed is True
    content = config.read_text()
    assert "image/png" in content
    assert "image/webp" in content


def test_apply_mime_patch_restores_on_validation_failure(tmp_path, monkeypatch):
    _backups(tmp_path, monkeypatch)
    config = tmp_path / "site.conf"
    original = "server {\n    server_name media.example.me;\n}\n"
    config.write_text(original)
    monkeypatch.setattr(serverfix, "run_control", Mock(side_effect=RuntimeError("bad syntax")))
    with pytest.raises(RuntimeError, match="restored after failed validation"):
        serverfix.apply_mime_patch("Nginx", config, "/usr/sbin/nginx")
    assert config.read_text() == original


def test_apply_mime_patch_raises_without_server_block(tmp_path, monkeypatch):
    _backups(tmp_path, monkeypatch)
    config = tmp_path / "site.conf"
    config.write_text("http {\n    include mime.types;\n}\n")
    monkeypatch.setattr(serverfix, "run_control", Mock())
    with pytest.raises(ValueError, match="Could not find a server block"):
        serverfix.apply_mime_patch("Nginx", config, "/usr/sbin/nginx")
    assert serverfix.MIME_MARKER not in config.read_text()


def test_correct_host_mime_applies_patch_automatically(monkeypatch):
    from termicast import hosting, prompts
    monkeypatch.setattr(serverfix, "detect_servers", lambda: {"Nginx": "/usr/sbin/nginx"})
    monkeypatch.setattr(prompts, "menu", lambda *a, **k: 1)
    monkeypatch.setattr(prompts, "text", lambda *a, **k: "/etc/nginx/sites-available/site")
    monkeypatch.setattr(prompts, "confirm", lambda *a, **k: True)
    applied = []
    monkeypatch.setattr(serverfix, "apply_mime_patch",
                        lambda name, path, exe: applied.append(path) or (Mock(), True))
    monkeypatch.setattr(serverfix, "reload_server", Mock())
    monkeypatch.setattr(hosting, "doctor", lambda *a: [])
    show = {"id": "001", "base_url": "https://media.example.me",
            "output_dir": "/var/www/site", "hosting": "local"}
    assert prompts.correct_host_mime(Mock(), show) is True
    assert applied == ["/etc/nginx/sites-available/site"]


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
