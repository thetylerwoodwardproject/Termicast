from copy import deepcopy

import pytest

from termicast.models import new_show, new_episode
from termicast.validation import validate_show, validate_episode
from termicast.storage import validate_storage, validate_conflicting_prefixes


def test_show_defaults_validate():
    show = new_show(title="S", description="d", base_url="https://e.org/show", output_dir="/tmp/x")
    assert validate_show(show) == []


def test_invalid_audio_preset():
    show = new_show(title="S", description="d", base_url="https://e.org/show",
                    output_dir="/tmp/x", audio_preset="loud")
    assert any("audio_preset" in e for e in validate_show(show))


def test_invalid_image_preset():
    show = new_show(title="S", description="d", base_url="https://e.org/show",
                    output_dir="/tmp/x", image_preset="tiny")
    assert any("image_preset" in e for e in validate_show(show))


def test_s3_storage_requires_bucket():
    show = new_show(title="S", description="d", base_url="https://e.org/show",
                    output_dir="/tmp/x", hosting="s3")
    assert any("bucket" in e for e in validate_storage(show))


def test_s3_storage_requires_asset_base_url():
    show = new_show(title="S", description="d", base_url="https://e.org/show",
                    output_dir="/tmp/x", hosting="s3", bucket="b")
    assert any("asset base URL" in e for e in validate_storage(show))
    show["asset_base_url"] = "https://cdn.e.org/show"
    assert validate_storage(show) == []


def test_mirror_feed_non_boolean_rejected():
    show = new_show(title="S", description="d", base_url="https://e.org/show",
                    output_dir="/tmp/x", hosting="s3", bucket="b",
                    asset_base_url="https://cdn.e.org/show", mirror_feed="yes")
    assert any("mirror_feed" in e for e in validate_storage(show))


def test_keep_local_media_non_boolean_rejected():
    show = new_show(title="S", description="d", base_url="https://e.org/show",
                    output_dir="/tmp/x", keep_local_media="yes")
    assert any("keep_local_media" in e for e in validate_storage(show))


def test_mirror_feed_boolean_accepted():
    show = new_show(title="S", description="d", base_url="https://e.org/show",
                    output_dir="/tmp/x", hosting="s3", bucket="b",
                    asset_base_url="https://cdn.e.org/show", mirror_feed=True)
    assert validate_storage(show) == []


def test_asset_base_uses_asset_base_url_for_s3():
    from termicast.storage import asset_base
    show = new_show(base_url="https://e.org/show", asset_base_url="https://cdn.e.org/show", hosting="s3")
    assert asset_base(show) == "https://cdn.e.org/show"
    show["hosting"] = "local"
    assert asset_base(show) == "https://e.org/show"


def test_s3_prefix_rules():
    show = new_show(title="S", description="d", base_url="https://e.org/show",
                    output_dir="/tmp/x", hosting="s3", bucket="b", prefix="/bad")
    assert any("prefix" in e for e in validate_storage(show))
    show["prefix"] = "good/prefix"
    assert not any("prefix" in e for e in validate_storage(show))


def test_conflicting_prefixes_rejected():
    a = new_show(hosting="s3", bucket="b", prefix="show", endpoint_url="https://s3.e.org")
    b = new_show(hosting="s3", bucket="b", prefix="show/extra", endpoint_url="https://s3.e.org")
    try:
        validate_conflicting_prefixes([a, b])
        assert False, "expected conflict"
    except ValueError as exc:
        assert "conflict" in str(exc)


def test_save_show_rejects_conflicting_s3_prefix(db, tmp_path):
    def s3_show(prefix, output_dir):
        return new_show(title="S", description="d", base_url="https://e.org/show",
                        output_dir=str(output_dir), hosting="s3", bucket="b", prefix=prefix,
                        endpoint_url="https://s3.e.org", asset_base_url="https://s3.e.org/b")

    db.save_show(s3_show("show", tmp_path / "a"))
    try:
        db.save_show(s3_show("show/extra", tmp_path / "b"))
        assert False, "expected conflict"
    except ValueError as exc:
        assert "conflict" in str(exc)


def test_episode_slug_validation():
    episode = new_episode(title="E", description="d", mp3_url="https://e.org/x.mp3",
                          length=1, duration=1.0, slug="s01e001")
    assert validate_episode(episode) == []
    episode["slug"] = "bad/slug"
    assert any("slug" in e for e in validate_episode(episode))


@pytest.mark.parametrize("duration", [None, 0, -1, 60])
def test_start_only_chapters_with_known_or_unknown_duration(duration):
    from termicast.validation import chapter_ends, validate_chapter_payload
    chapters = [{"startTime": 0, "title": "Opening"}, {"startTime": 30, "title": "Main"}]
    original = deepcopy(chapters)
    episode = new_episode(title="E", description="d", mp3_url="https://e.org/e.mp3",
                          length=1, duration=duration, chapters=chapters)
    assert not [e for e in validate_episode(episode) if e.startswith("chapters")]
    assert validate_chapter_payload({"chapters": chapters}, episode) == original
    assert list(chapter_ends(chapters, duration)) == [30, 60 if duration == 60 else None]
    assert chapters == original


@pytest.mark.parametrize("chapters,message", [
    ([{"startTime": 0}, {"startTime": 0}], "sorted by startTime"),
    ([{"startTime": 30}, {"startTime": 10}], "sorted by startTime"),
    ([{"startTime": 61}], "start within media duration"),
    ([{"startTime": -1}], "nonnegative finite"),
    ([{"startTime": float("inf")}], "nonnegative finite"),
    ([{"startTime": 0, "endTime": 31}, {"startTime": 30}], "next startTime"),
    ([{"startTime": 0, "endTime": 61}], "end within media duration"),
    ([{"startTime": 0, "endTime": None}], "finite and greater than start"),
    ([{"startTime": 0, "endTime": float("nan")}], "finite and greater than start"),
    ([{"startTime": 10, "endTime": 10}], "finite and greater than start"),
])
def test_chapter_timing_validation(chapters, message):
    from termicast.validation import validate_chapter_payload
    with pytest.raises(ValueError, match=message):
        validate_chapter_payload({"chapters": chapters}, {"duration": 60})


def test_explicit_chapter_gap_preserved():
    from termicast.validation import chapter_ends, validate_chapter_payload
    chapters = [{"startTime": 0, "endTime": 20}, {"startTime": 30}]
    original = deepcopy(chapters)
    assert validate_chapter_payload({"chapters": chapters}, {"duration": 60}) == original
    assert list(chapter_ends(chapters, 60)) == [20, 60]
    assert chapters == original


@pytest.mark.parametrize("format,mode,size,chapter", [
    ("WEBP", "RGB", (1400, 1400), False),
    ("PNG", "RGBA", (3000, 3000), False),
    ("JPEG", "RGB", (100, 50), False),
    ("WEBP", "RGB", (100, 50), True),
])
def test_artwork_conversion_signal(monkeypatch, format, mode, size, chapter):
    from io import BytesIO
    from PIL import Image
    from termicast import validation
    content = BytesIO()
    Image.new(mode, size).save(content, format)
    monkeypatch.setattr(validation, "_download", lambda url, target, limit: target.write(content.getvalue()))
    with pytest.raises(validation.ArtworkNeedsConversion) as caught:
        validation.inspect_artwork("https://e.org/art", chapter=chapter)
    exc = caught.value
    assert (exc.format, exc.mode, exc.size) == (format, mode, size)
    assert exc.required_size == (None if chapter else (3000, 3000))
