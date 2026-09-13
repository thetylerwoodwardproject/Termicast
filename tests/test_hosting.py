from unittest.mock import Mock

import pytest

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


def test_s3_write_policy_snippet_scopes_to_bucket_and_prefix():
    import json
    show = new_show(base_url="https://e.org/show", hosting="s3", bucket="my-bucket", prefix="my-show")
    policy = json.loads(hosting.s3_write_policy_snippet(show))
    actions = {action for statement in policy["Statement"] for action in
               ([statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"])}
    assert actions == {"s3:ListBucket", "s3:GetObject", "s3:PutObject", "s3:DeleteObject"}
    resources = [statement["Resource"] for statement in policy["Statement"]]
    assert "arn:aws:s3:::my-bucket" in resources
    assert "arn:aws:s3:::my-bucket/my-show/*" in resources


def test_s3_write_policy_snippet_without_prefix_uses_wildcard():
    import json
    show = new_show(base_url="https://e.org/show", hosting="s3", bucket="my-bucket", prefix="")
    policy = json.loads(hosting.s3_write_policy_snippet(show))
    resources = [statement["Resource"] for statement in policy["Statement"]]
    assert "arn:aws:s3:::my-bucket/*" in resources


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


def test_check_assets_flags_unreachable_show_artwork(monkeypatch):
    """The show's own cover art must be verified, not just episode artwork.

    Regression test: check_assets used to only look at episode artwork_url,
    so a show's cover image that never made it to hosting went unnoticed by
    doctor/deploy verification.
    """
    show = new_show(base_url="https://e.org/show", artwork_url="https://e.org/show/images/cover.jpg")
    monkeypatch.setattr(hosting, "check_url", lambda url, expected=None: [f"Unreachable: {url}"])
    problems = hosting.check_assets(show, [])
    assert any("images/cover.jpg" in p for p in problems)


def test_check_assets_flags_unreachable_chapter_image(monkeypatch):
    """Each chapter's image must be verified, not just the chapters.json file."""
    show = new_show(base_url="https://e.org/show")
    episode = {
        "status": "published", "guid": "ep-1",
        "mp3_url": "https://e.org/show/audio/e.mp3",
        "artwork_url": "https://e.org/show/images/e.jpg",
        "chapters": [{"startTime": 0, "endTime": 60, "title": "S",
                      "img": "https://e.org/show/images/chapters/c1.jpg"}],
    }
    monkeypatch.setattr(hosting, "check_url", lambda url, expected=None: [f"Unreachable: {url}"])
    problems = hosting.check_assets(show, [episode])
    assert any("images/chapters/c1.jpg" in p for p in problems)


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


def test_summarize_caps_mime_errors_with_count():
    problems = [f"Unexpected Content-Type 'text/html' (expected text/vtt): https://e.org/x{i}"
                for i in range(44)]
    lines = hosting.summarize_verification_problems(problems, target="local")
    assert len([l for l in lines if "Unexpected Content-Type" in l]) == 5
    assert any("39 more Content-Type problems omitted" in l for l in lines)
    assert any("Nginx MIME snippet" in l for l in lines)


def test_summarize_preserves_non_mime_order_and_details():
    problems = [
        "Unreachable: https://e.org/a",
        "Unexpected Content-Type 'text/html' (expected text/vtt): https://e.org/b",
        "HTTP 500: https://e.org/c",
    ]
    lines = hosting.summarize_verification_problems(problems, target="local")
    assert lines[0] == "Unreachable: https://e.org/a"
    assert lines[1] == "HTTP 500: https://e.org/c"
    assert lines[2].startswith("Unexpected Content-Type")
    assert any("must be fixed before" in l for l in lines)


def test_summarize_missing_content_type_is_mime():
    problems = ["Missing Content-Type (expected text/vtt): https://e.org/x"]
    lines = hosting.summarize_verification_problems(problems, target="local")
    assert any("Missing Content-Type" in l for l in lines)


def test_summarize_recognizes_show_prefixed_mime():
    problems = ["My Show (001): Unexpected Content-Type 'text/html' (expected text/vtt): https://e.org/x"]
    lines = hosting.summarize_verification_problems(problems, target="mixed")
    assert any("Unexpected Content-Type" in l for l in lines)


def test_summarize_treats_no_content_type_mapping_as_non_mime():
    problems = ["feed.xml: No Content-Type mapping for feed.xml; use a supported format"]
    lines = hosting.summarize_verification_problems(problems, target="s3")
    assert lines == problems
    assert not any("Nginx MIME snippet" in l for l in lines)
    assert not any("Uploads already set Content-Type" in l for l in lines)


def test_summarize_preserves_duplicate_counts():
    problems = ["Unexpected Content-Type 'text/html' (expected text/vtt): https://e.org/x"] * 10
    lines = hosting.summarize_verification_problems(problems, limit=3, target="local")
    assert any("7 more Content-Type problems omitted" in l for l in lines)


def test_summarize_empty_and_zero_limit():
    assert hosting.summarize_verification_problems([]) == []
    problems = ["Unexpected Content-Type 'text/html' (expected text/vtt): https://e.org/x"] * 3
    lines = hosting.summarize_verification_problems(problems, limit=0, target="local")
    assert all("Unexpected Content-Type" not in l for l in lines)
    assert any("3 more Content-Type problems omitted" in l for l in lines)


def test_summarize_rejects_negative_limit():
    with pytest.raises(ValueError):
        hosting.summarize_verification_problems([], limit=-1)


def test_summarize_rejects_bad_target():
    with pytest.raises(ValueError):
        hosting.summarize_verification_problems([], target="bogus")


def test_summarize_s3_target_guidance():
    problems = ["Unexpected Content-Type 'text/html' (expected audio/mpeg): https://cdn.e.org/x.mp3"]
    lines = hosting.summarize_verification_problems(problems, target="s3")
    assert any("Uploads already set Content-Type" in l for l in lines)
    assert not any("Nginx MIME snippet" in l for l in lines)


def test_summarize_mixed_target_guidance():
    problems = ["Missing Content-Type (expected text/vtt): https://e.org/x"]
    lines = hosting.summarize_verification_problems(problems, target="mixed")
    assert any("Nginx MIME snippet" in l for l in lines)
    assert any("already set Content-Type" in l for l in lines)


def test_summarize_does_not_mutate_input():
    problems = ["Unexpected Content-Type 'text/html': https://e.org/x"]
    original = list(problems)
    hosting.summarize_verification_problems(problems, target="local")
    assert problems == original
