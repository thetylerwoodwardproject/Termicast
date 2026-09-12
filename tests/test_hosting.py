from unittest.mock import Mock

from termicast import hosting
from termicast.models import new_show


def test_nginx_snippet_has_four_types():
    show = new_show(base_url="https://e.org/show")
    snippet = hosting.nginx_snippet(show)
    for content_type in ("application/rss+xml", "application/json+chapters",
                         "text/vtt", "audio/mp4"):
        assert content_type in snippet


def test_apache_snippet_has_four_types():
    show = new_show(base_url="https://e.org/show")
    snippet = hosting.apache_snippet(show)
    for content_type in ("application/rss+xml", "application/json+chapters",
                         "text/vtt", "audio/mp4"):
        assert content_type in snippet


def test_check_url_ok(monkeypatch):
    monkeypatch.setattr(hosting, "_head_or_range", lambda url, expected=None: (200, "text/vtt", b"x"))
    assert hosting.check_url("https://e.org/t.vtt", "text/vtt") == []


def test_check_url_distinguishes_errors(monkeypatch):
    monkeypatch.setattr(hosting, "_head_or_range", lambda url, expected=None: (404, "", b""))
    problems = hosting.check_url("https://e.org/missing", "text/vtt")
    assert any("404" in p for p in problems)


def test_check_url_unexpected_content_type(monkeypatch):
    monkeypatch.setattr(hosting, "_head_or_range", lambda url, expected=None: (200, "text/html", b""))
    problems = hosting.check_url("https://e.org/x.vtt", "text/vtt")
    assert any("Content-Type" in p for p in problems)


def test_check_url_unreachable(monkeypatch):
    monkeypatch.setattr(hosting, "_head_or_range", lambda url, expected=None: (None, "", b""))
    problems = hosting.check_url("https://e.org/x", "text/vtt")
    assert any("Unreachable" in p for p in problems)


def test_doctor_aggregates_and_unknown_show():
    db = Mock()
    db.get_show.return_value = None
    assert hosting.doctor(db, "missing") == ["Unknown podcast ID: missing"]


def test_doctor_calls_checks(monkeypatch):
    db = Mock()
    show = new_show(id="001", title="S", base_url="https://e.org/s", output_dir="/tmp/x")
    db.get_show.return_value = show
    db.list_episodes.return_value = []
    monkeypatch.setattr(hosting, "check_tools", lambda s: ["tool problem"])
    monkeypatch.setattr(hosting, "check_feed", lambda s: ["feed problem"])
    monkeypatch.setattr(hosting, "check_assets", lambda s, e: ["asset problem"])
    problems = hosting.doctor(db, show["id"])
    assert len(problems) == 3
    assert any("tool problem" in p for p in problems)
    assert any("feed problem" in p for p in problems)
    assert any("asset problem" in p for p in problems)
