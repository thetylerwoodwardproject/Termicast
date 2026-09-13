"""Cancelling a field unwinds to the enclosing menu without changing the record."""

from unittest.mock import Mock
import shutil

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


def test_chapters_sort_without_stop_or_empty_optional_fields(monkeypatch):
    actions = iter([1, 1, 1, 4])
    values = iter(["20", "Outro", "", "", "0", "Opening", "", "", "10", "Main", "", ""])
    labels = []
    displays = []
    monkeypatch.setattr(prompts, "menu", lambda *a, **k: next(actions))
    monkeypatch.setattr(prompts, "text", lambda label, *a, **k: labels.append(label) or next(values))
    monkeypatch.setattr(prompts, "review", lambda name, data: displays.append(data))
    chapters = prompts._segments([], False, episode={"duration": 30})
    assert chapters == [{"startTime": 0, "title": "Opening"}, {"startTime": 10, "title": "Main"},
                        {"startTime": 20, "title": "Outro"}]
    assert not any("Stop" in label for label in labels)
    assert displays[-1]["1"] == "00:00:00 → 00:00:10   Opening"
    assert displays[-1]["3"] == "00:00:20 → 00:00:30   Outro"


def test_duplicate_chapter_start_reprompts(monkeypatch):
    actions = iter([1, 4])
    values = iter(["0", "Duplicate", "", "", "10", "Second", "", ""])
    errors = []
    monkeypatch.setattr(prompts, "menu", lambda *a, **k: next(actions))
    monkeypatch.setattr(prompts, "text", lambda *a, **k: next(values))
    monkeypatch.setattr(prompts, "error", errors.append)
    chapters = prompts._segments([{"startTime": 0, "title": "First"}], False)
    assert [c["startTime"] for c in chapters] == [0, 10]
    assert errors == ["Another chapter already starts at 00:00:00"]


def test_chapter_edit_drops_stale_ends_but_preserves_other_gaps(monkeypatch):
    actions = iter([2, 3, 4])  # Edit third chapter, moving it before the second.
    values = iter(["15", "Moved", "", ""])
    monkeypatch.setattr(prompts, "menu", lambda *a, **k: next(actions))
    monkeypatch.setattr(prompts, "text", lambda *a, **k: next(values))
    existing = [{"startTime": 0, "endTime": 20, "title": "First"},
                {"startTime": 20, "endTime": 25, "title": "Gap"},
                {"startTime": 30, "endTime": 60, "title": "Last", "img": "", "url": ""}]
    chapters = prompts._segments(existing, False)
    assert chapters == [{"startTime": 0, "title": "First"}, {"startTime": 15, "title": "Moved"},
                        {"startTime": 20, "endTime": 25, "title": "Gap"}]
    assert existing[0]["endTime"] == 20
    assert existing[2]["endTime"] == 60


def test_chapter_title_limit(monkeypatch):
    values = iter(["0", "x" * 256, "Valid", "", ""])
    errors = []
    monkeypatch.setattr(prompts, "text", lambda *a, **k: next(values))
    monkeypatch.setattr(prompts, "error", errors.append)
    assert prompts._chapter_entry({}, None, {})["title"] == "Valid"
    assert errors


@pytest.mark.parametrize("slug", ["ep001", ""])
def test_local_chapter_art_uses_free_index_after_sorting(tmp_path, monkeypatch, slug):
    from pathlib import Path
    from PIL import Image
    from termicast.models import new_episode, new_show, chapter_image_relative
    show = new_show(base_url="https://e.org/show", output_dir=str(tmp_path / "out"))
    episode = new_episode(guid="opaque/imported/id", slug=slug, duration=60)
    existing = []
    for start, index in [(0, 3), (30, 1)]:
        relative = chapter_image_relative(slug, index, ".jpg", guid=episode["guid"])
        existing.append({"startTime": start, "title": str(start), "img": "https://e.org/show/" + relative})
        path = tmp_path / "out" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"existing")
    source = tmp_path / "new.webp"
    Image.new("RGB", (100, 50)).save(source)
    values = iter(["10", "New", str(source), ""])
    actions = iter([1, 4])
    monkeypatch.setattr(prompts, "menu", lambda *a, **k: next(actions))
    monkeypatch.setattr(prompts, "text", lambda *a, **k: next(values))
    result = prompts._segments(existing, False, show=show, episode=episode)
    relative = chapter_image_relative(slug, 2, ".jpg", guid=episode["guid"])
    assert result[1]["img"] == "https://e.org/show/" + relative
    with Image.open(tmp_path / "out" / relative) as image:
        assert (image.format, image.size) == ("JPEG", (100, 50))
    assert (tmp_path / "out" / Path(existing[0]["img"].removeprefix("https://e.org/show/"))).read_bytes() == b"existing"


@pytest.mark.parametrize("accept", [True, False])
def test_remote_webp_artwork_conversion_offer(tmp_path, monkeypatch, accept):
    from io import BytesIO
    from PIL import Image
    from termicast import validation
    from termicast.models import new_show
    show = new_show(output_dir=str(tmp_path / "out"), base_url="https://e.org/show")
    content = BytesIO()
    Image.new("RGB", (1400, 1400)).save(content, "WEBP")
    monkeypatch.setattr(validation, "_download", lambda url, handle, limit: handle.write(content.getvalue()))
    values = iter(["https://source.e.org/cover.webp", ""])
    confirms = []
    monkeypatch.setattr(prompts, "text", lambda *a, **k: next(values))
    monkeypatch.setattr(prompts, "confirm", lambda label, *a: confirms.append(label) or
                        (accept if "Convert and host" in label else True))
    result = prompts._artwork("", False, show=show)
    assert "Convert and host this artwork?" in confirms
    if accept:
        assert result == "https://e.org/show/images/cover.jpg"
        assert validation.inspect_local_artwork(tmp_path / "out/images/cover.jpg") == []
    else:
        assert result == ""
        assert not (tmp_path / "out/images/cover.jpg").exists()


def test_show_artwork_avoids_existing_episode_cover(tmp_path, monkeypatch):
    from PIL import Image
    from termicast.models import new_show
    show = new_show(output_dir=str(tmp_path / "out"), base_url="https://e.org/show")
    image = tmp_path / "out/images/cover.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"episode art")
    source = tmp_path / "cover.webp"
    Image.new("RGB", (50, 50)).save(source)
    monkeypatch.setattr(prompts, "text", lambda *a, **k: str(source))
    prompts.edit_field(show, "artwork_url")
    assert show["artwork_url"] == "https://e.org/show/images/cover-2.jpg"
    assert image.read_bytes() == b"episode art"


def test_show_artwork_avoids_episode_cover_evicted_from_local_disk(tmp_path, monkeypatch):
    from PIL import Image
    from termicast.models import new_show
    show = new_show(output_dir=str(tmp_path / "out"), base_url="https://e.org/show",
                    hosting="s3", asset_base_url="https://cdn.e.org/show")
    feed = tmp_path / "out/feed.xml"
    feed.parent.mkdir()
    feed.write_text('<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
                    '<channel><item><itunes:image href="https://cdn.e.org/show/images/cover.jpg"/>'
                    '</item></channel></rss>')
    source = tmp_path / "cover.webp"
    Image.new("RGB", (50, 50)).save(source)
    monkeypatch.setattr(prompts, "text", lambda *a, **k: str(source))
    prompts.edit_field(show, "artwork_url")
    assert show["artwork_url"] == "https://cdn.e.org/show/images/cover-2.jpg"


def test_chapter_image_index_claims_ignore_suffix_and_allow_own_index(tmp_path, monkeypatch):
    from PIL import Image
    from termicast.models import new_episode, new_show
    show = new_show(output_dir=str(tmp_path / "out"), base_url="https://e.org/show")
    old = {"startTime": 0, "title": "Old", "img": "https://e.org/show/images/chapters/ep001-02.jpg"}
    other = {"startTime": 10, "title": "Other", "img": "https://e.org/show/images/chapters/ep001-01.png"}
    episode = new_episode(slug="ep001", chapters=[old, other])
    source = tmp_path / "new.webp"
    Image.new("RGB", (50, 50)).save(source)
    values = iter(["0", "Edited", str(source), ""])
    monkeypatch.setattr(prompts, "text", lambda *a, **k: next(values))
    entry = prompts._chapter_entry(old, show, episode)
    assert entry["img"] == old["img"]


def test_episode_artwork_updates_managed_path(tmp_path, monkeypatch):
    from PIL import Image
    from termicast.models import new_episode, new_show, chapter_filename
    show = new_show(output_dir=str(tmp_path / "out"), base_url="https://e.org/show")
    episode = new_episode(guid="external/id", artwork_url="https://e.org/show/images/old.png",
                          image_path="images/old.png")
    source = tmp_path / "cover.webp"
    Image.new("RGB", (50, 50)).save(source)
    monkeypatch.setattr(prompts, "text", lambda *a, **k: str(source))
    prompts.edit_field(episode, "artwork_url", episode=True, show=show)
    relative = "images/" + chapter_filename(episode["guid"]).removesuffix(".json") + ".jpg"
    assert episode["artwork_url"] == "https://e.org/show/" + relative
    assert episode["image_path"] == relative


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="FFmpeg required")
def test_add_webp_episode_and_manual_chapters_through_publication(db, show, tmp_path, monkeypatch, capsys):
    import json
    from pathlib import Path
    from PIL import Image
    from conftest import make_wav
    from termicast.feed import validate_feed
    from termicast.publisher import Publisher, episode_asset_paths
    from termicast.validation import inspect_local_artwork, validate_episode
    audio = make_wav(tmp_path / "episode.wav", seconds=2)
    cover = tmp_path / "cover.webp"
    Image.new("RGB", (3000, 3000)).save(cover)
    chapter_art = tmp_path / "chapter.webp"
    Image.new("RGB", (100, 50)).save(chapter_art)
    actions = iter([5, 2, 1, 1, 1, 4, 2])
    values = iter(["Episode", "Description", "1.5", "Outro", "", "",
                   "0", "Intro", str(chapter_art), "", "0.5", "Main", "", ""])
    labels = []
    monkeypatch.setattr(prompts, "menu", lambda *a, **k: next(actions))
    monkeypatch.setattr(prompts, "text", lambda label, *a, **k: labels.append(label) or next(values))
    monkeypatch.setattr(prompts, "confirm", lambda *a, **k: True)
    prompts.add_episode(db, Publisher(db), show, [audio, cover], slug="ep001")
    saved = db.list_episodes(show["id"])[0]
    assert saved["status"] == "published"
    assert validate_episode(saved) == []
    assert [chapter["startTime"] for chapter in saved["chapters"]] == [0, 0.5, 1.5]
    assert all("endTime" not in chapter for chapter in saved["chapters"])
    assert saved["chapters"][0]["img"].endswith("/images/chapters/ep001-01.jpg")
    assert "images/chapters/ep001-01.jpg" in episode_asset_paths(saved, show)
    root = Path(show["output_dir"])
    assert inspect_local_artwork(root / "images/ep001.jpg", episode=True) == []
    assert json.loads((root / "chapters/ep001.json").read_text())["chapters"] == saved["chapters"]
    assert validate_feed((root / "feed.xml").read_bytes()) == []
    assert not any("Stop" in label for label in labels)
    assert "WEBP to JPEG" in capsys.readouterr().out


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
