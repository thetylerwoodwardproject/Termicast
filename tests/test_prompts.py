"""Cancelling a field unwinds to the enclosing menu without changing the record."""

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
            "output_dir": "/srv/show"}
    prompts.hosting_form(data)
    assert data == {"hosting": "local", "output_dir": "/srv/show"}


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
