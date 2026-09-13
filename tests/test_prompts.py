"""Cancelling a field unwinds to the enclosing menu without changing the record."""

from unittest.mock import Mock

import pytest

from termicast import prompts
from termicast.prompts import Cancelled


@pytest.fixture
def cancel_text(monkeypatch):
    """Make every text field raise Cancelled, as Escape or Ctrl-C does."""
    def raiser(*args, **kwargs):
        raise Cancelled()
    monkeypatch.setattr(prompts, "text", raiser)


def test_text_cancel_is_not_an_exception_subclass():
    # The broad `except Exception` handlers around menu actions must not
    # report a deliberate cancel as a failure.
    assert issubclass(Cancelled, BaseException)
    assert not issubclass(Cancelled, Exception)


def test_edit_menu_leaves_the_field_unchanged(monkeypatch, cancel_text):
    monkeypatch.setattr(prompts, "menu", lambda *a, **k: 1)
    data = {"title": "Original", "description": "Kept"}
    prompts.edit_menu(data, ("title", "description"))
    assert data == {"title": "Original", "description": "Kept"}


def test_hosting_form_cancel_leaves_settings_untouched(monkeypatch, cancel_text):
    monkeypatch.setattr(prompts, "menu", lambda *a, **k: 2)  # S3-compatible storage
    data = {"hosting": "local", "output_dir": "/srv/show"}
    with pytest.raises(Cancelled):
        prompts.hosting_form(data)
    assert data == {"hosting": "local", "output_dir": "/srv/show"}


def test_hosting_form_invalid_settings_leave_settings_untouched(monkeypatch):
    monkeypatch.setattr(prompts, "menu", lambda *a, **k: 2)
    monkeypatch.setattr(prompts, "text", lambda *a, **k: "")
    monkeypatch.setattr(prompts, "confirm", lambda *a, **k: False)
    data = {"hosting": "local", "output_dir": "/srv/show"}
    with pytest.raises(ValueError):
        prompts.hosting_form(data)
    assert data == {"hosting": "local", "output_dir": "/srv/show"}


def test_hosting_form_drops_stale_s3_settings_when_switching_to_local(monkeypatch):
    monkeypatch.setattr(prompts, "menu", lambda *a, **k: 1)  # Local web server
    data = {"hosting": "s3", "bucket": "old-bucket", "prefix": "old", "enabled": True,
            "mirror_feed": True, "output_dir": "/srv/show"}
    prompts.hosting_form(data)
    assert data == {"hosting": "local", "output_dir": "/srv/show"}


def test_hosting_form_asks_mirror_feed_for_s3(monkeypatch):
    monkeypatch.setattr(prompts, "menu", lambda *a, **k: 2)  # S3-compatible storage

    def fake_text(label, current="", required=False, **kwargs):
        if label == "Bucket name":
            return "b"
        if "asset" in label.lower():
            return "https://cdn.e.org/show"
        return current or ""

    monkeypatch.setattr(prompts, "text", fake_text)
    confirmed = []
    monkeypatch.setattr(prompts, "confirm",
                        lambda label, default=False: confirmed.append(label) or False)
    monkeypatch.setattr(prompts, "_ensure_s3_credentials", lambda: None)
    data = {"hosting": "local", "output_dir": "/srv/show"}
    prompts.hosting_form(data)
    assert any("feed.xml" in label for label in confirmed)
    assert data["mirror_feed"] is False
    assert data["hosting"] == "s3"


def test_show_form_cancel_abandons_creation(cancel_text, monkeypatch):
    monkeypatch.setattr(prompts, "menu", lambda *a, **k: 1)
    assert prompts.show_form({"base_url": "https://example.org/show"}, collect=True) is None


def test_schedule_time_cancel_returns_no_time(cancel_text):
    assert prompts.schedule_time({"timezone": "UTC"}) is None


def test_podroll_cancel_keeps_existing_entries(monkeypatch, cancel_text):
    actions = iter([1, 4])  # Add entry, then Done.
    monkeypatch.setattr(prompts, "menu", lambda *a, **k: next(actions))
    existing = [{"feedGuid": "kept", "feedUrl": "", "title": "Kept"}]
    assert prompts._podroll(existing) == existing


def test_segments_cancel_keeps_existing_entries(monkeypatch):
    actions = iter([1, 4])  # Add entry, then Done.
    monkeypatch.setattr(prompts, "menu", lambda *a, **k: next(actions))

    def raiser(*args, **kwargs):
        raise Cancelled()
    monkeypatch.setattr(prompts, "number", raiser)
    existing = [{"startTime": 0, "endTime": 10, "title": "Kept"}]
    assert prompts._segments(existing, soundbites=False) == existing


def test_secret_masks_input_when_not_a_tty(monkeypatch):
    monkeypatch.setattr(prompts.sys.stdin, "isatty", lambda: False)
    calls = []
    monkeypatch.setattr(prompts.console, "input",
                        lambda prompt, password=False: calls.append((prompt, password)) or "typed-secret")
    assert prompts.secret("Access key") == "typed-secret"
    assert calls and calls[0][1] is True


def test_ensure_s3_credentials_skips_when_credentials_present(monkeypatch):
    from termicast import s3deploy
    monkeypatch.setattr(s3deploy, "s3_credentials_present", lambda: True)
    asked = []
    monkeypatch.setattr(prompts, "confirm", lambda *a, **k: asked.append(1) or True)
    prompts._ensure_s3_credentials()
    assert asked == []


def test_ensure_s3_credentials_warns_without_overwriting_existing_invalid_file(monkeypatch, tmp_path):
    from termicast import s3deploy
    cfg = tmp_path / ".s3cfg"
    cfg.write_text("garbage, no [default] section")
    monkeypatch.setattr(s3deploy, "s3_credentials_present", lambda: False)
    monkeypatch.setattr(s3deploy, "s3cfg_path", lambda: cfg)
    asked = []
    monkeypatch.setattr(prompts, "confirm", lambda *a, **k: asked.append(1) or True)
    prompts._ensure_s3_credentials()
    assert asked == []
    assert cfg.read_text() == "garbage, no [default] section"


def test_ensure_s3_credentials_declines_setup(monkeypatch, tmp_path):
    from termicast import s3deploy
    cfg = tmp_path / ".s3cfg"
    monkeypatch.setattr(s3deploy, "s3_credentials_present", lambda: False)
    monkeypatch.setattr(s3deploy, "s3cfg_path", lambda: cfg)
    written = []
    monkeypatch.setattr(s3deploy, "write_s3cfg", lambda *a, **k: written.append((a, k)))
    monkeypatch.setattr(prompts, "confirm", lambda *a, **k: False)
    prompts._ensure_s3_credentials()
    assert written == []
    assert not cfg.exists()


def test_ensure_s3_credentials_creates_file_when_accepted(monkeypatch, tmp_path):
    from termicast import s3deploy
    cfg = tmp_path / ".s3cfg"
    monkeypatch.setattr(s3deploy, "s3_credentials_present", lambda: False)
    monkeypatch.setattr(s3deploy, "s3cfg_path", lambda: cfg)
    written = []
    monkeypatch.setattr(s3deploy, "write_s3cfg", lambda ak, sk: written.append((ak, sk)))
    monkeypatch.setattr(prompts, "confirm", lambda *a, **k: True)
    keys = iter(["my-access-key", "my-secret-key"])
    monkeypatch.setattr(prompts, "secret", lambda label: next(keys))
    prompts._ensure_s3_credentials()
    assert written == [("my-access-key", "my-secret-key")]


def test_ensure_s3_credentials_requires_both_keys(monkeypatch, tmp_path):
    from termicast import s3deploy
    cfg = tmp_path / ".s3cfg"
    monkeypatch.setattr(s3deploy, "s3_credentials_present", lambda: False)
    monkeypatch.setattr(s3deploy, "s3cfg_path", lambda: cfg)
    written = []
    monkeypatch.setattr(s3deploy, "write_s3cfg", lambda ak, sk: written.append((ak, sk)))
    monkeypatch.setattr(prompts, "confirm", lambda *a, **k: True)
    keys = iter(["my-access-key", ""])
    monkeypatch.setattr(prompts, "secret", lambda label: next(keys))
    prompts._ensure_s3_credentials()
    assert written == []


def test_set_s3_credentials_replaces_existing_file(monkeypatch, tmp_path):
    from termicast import s3deploy
    cfg = tmp_path / ".s3cfg"
    cfg.write_text("[default]\naccess_key = old\nsecret_key = old\n")
    monkeypatch.setattr(s3deploy, "s3_credentials_present", lambda: True)
    monkeypatch.setattr(s3deploy, "s3cfg_path", lambda: cfg)
    monkeypatch.delenv("S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("S3_SECRET_KEY", raising=False)
    written = []
    monkeypatch.setattr(s3deploy, "write_s3cfg", lambda ak, sk: written.append((ak, sk)))
    confirms = []
    monkeypatch.setattr(prompts, "confirm", lambda *a, **k: confirms.append(1) or True)
    keys = iter(["new-access", "new-secret"])
    monkeypatch.setattr(prompts, "secret", lambda label: next(keys))
    prompts.set_s3_credentials()
    assert written == [("new-access", "new-secret")]
    assert confirms, "should confirm before overwriting an existing file"


def test_set_s3_credentials_declines_without_changes(monkeypatch, tmp_path):
    from termicast import s3deploy
    cfg = tmp_path / ".s3cfg"
    cfg.write_text("[default]\naccess_key = old\nsecret_key = old\n")
    monkeypatch.setattr(s3deploy, "s3_credentials_present", lambda: True)
    monkeypatch.setattr(s3deploy, "s3cfg_path", lambda: cfg)
    monkeypatch.delenv("S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("S3_SECRET_KEY", raising=False)
    written = []
    monkeypatch.setattr(s3deploy, "write_s3cfg", lambda ak, sk: written.append((ak, sk)))
    monkeypatch.setattr(prompts, "confirm", lambda *a, **k: False)
    monkeypatch.setattr(prompts, "secret", lambda label: "x")
    prompts.set_s3_credentials()
    assert written == []


def test_hosting_menu_s3_credentials_action(monkeypatch):
    from termicast import prompts
    choices = iter([11, 9])
    monkeypatch.setattr(prompts, "menu", lambda *a, **k: next(choices))
    called = []
    monkeypatch.setattr(prompts, "set_s3_credentials", lambda: called.append(1))
    prompts.hosting_menu(Mock(), Mock(), {"id": "001"})
    assert called == [1]
