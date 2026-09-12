from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image
from lxml import etree

from termicast.importer import import_feed, extract_episodes
from termicast.feed import render_feed, validate_feed, NS
from termicast.models import new_show


FIXTURE = Path(__file__).parent / "fixtures" / "sample.xml"


def test_import_feed_returns_identity_and_template():
    settings, template = import_feed(str(FIXTURE))
    assert settings["title"] == "Representative & Friends"
    assert settings["guid"] == "5eaf7b5e-cc24-5e12-9c73-96c1f81c3b12"
    assert template == FIXTURE.read_bytes()


def test_extract_episodes_preserves_guid_and_metadata():
    _, template = import_feed(str(FIXTURE))
    episodes = extract_episodes(template)
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode["guid"] == "rss-host-original-id-42"
    assert episode["mp3_url"] == "https://example.org/42.mp3"
    assert episode["duration"] == 3723.0


def test_render_preserves_unknown_xml():
    settings, template = import_feed(str(FIXTURE))
    show = new_show(**settings)
    show.update(settings)
    show["output_dir"] = "/tmp/unused"
    show["base_url"] = "https://new.example.org/podcast"
    episodes = extract_episodes(template)
    rendered = render_feed(show, template, episodes, datetime.now(timezone.utc))
    after = etree.fromstring(rendered)
    custom = after.find("channel/{https://example.org/extensions}metadata", NS)
    assert custom is not None
    assert after.findtext("channel/podcast:guid", namespaces=NS) == show["guid"]
    assert after.find("channel/atom:link[@rel='self']", NS).get("href") == "https://new.example.org/podcast/feed.xml"
    assert validate_feed(rendered) == []


def test_op3_detected_from_feed(tmp_path):
    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
        '<channel><title>S</title><description>d</description><link>https://old.example.org</link>'
        '<item><title>Ep</title><guid isPermaLink="false">ep-1</guid>'
        '<enclosure url="https://op3.dev/e/https://old.example.org/1.mp3" length="1" type="audio/mpeg"/>'
        '<pubDate>Sun, 01 Sep 2024 12:00:00 GMT</pubDate></item>'
        '</channel></rss>'
    )
    path = tmp_path / "feed.xml"
    path.write_text(feed, encoding="utf-8")
    settings, _ = import_feed(str(path))
    assert settings["op3"] is True


def test_op3_absent_without_prefix(tmp_path):
    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
        '<channel><title>S</title><description>d</description><link>https://old.example.org</link>'
        '<item><title>Ep</title><guid isPermaLink="false">ep-1</guid>'
        '<enclosure url="https://old.example.org/1.mp3" length="1" type="audio/mpeg"/>'
        '<pubDate>Sun, 01 Sep 2024 12:00:00 GMT</pubDate></item>'
        '</channel></rss>'
    )
    path = tmp_path / "feed.xml"
    path.write_text(feed, encoding="utf-8")
    settings, _ = import_feed(str(path))
    assert settings["op3"] is False


def test_artwork_auto_mode_is_per_import(tmp_path, monkeypatch):
    from termicast import cli
    from termicast.importer import _convert_import_artwork

    prompts = []
    monkeypatch.setattr(cli, "menu", lambda *args: prompts.append(args) or 2)
    reviewer = cli._artwork_reviewer()
    for index in range(2):
        path = tmp_path / f"{index}.png"
        Image.new("L", (10, 10), 120).save(path)
        _convert_import_artwork(path, str(path), reviewer)
        with Image.open(path) as image:
            assert image.mode == "RGB"
            assert image.getpixel((0, 0)) == (120, 120, 120)
    assert len(prompts) == 1
    assert cli._artwork_reviewer()("new.png", "PNG", "RGBA", (1400, 1400))
    assert len(prompts) == 2


def test_rgb_artwork_is_preserved(tmp_path):
    from termicast.importer import _convert_import_artwork

    path = tmp_path / "rgb.png"
    Image.new("RGB", (10, 10)).save(path)
    original = path.read_bytes()
    _convert_import_artwork(path, "image.png", lambda *args: pytest.fail("Unexpected prompt"))
    assert path.read_bytes() == original


def test_converted_artwork_still_requires_valid_dimensions(tmp_path):
    from termicast.importer import _convert_import_artwork
    from termicast.validation import inspect_local_artwork

    path = tmp_path / "small.png"
    Image.new("RGBA", (10, 10)).save(path)
    _convert_import_artwork(path, "small.png", lambda *args: True)
    with pytest.raises(ValueError, match="Show artwork must be square"):
        inspect_local_artwork(path)


@pytest.mark.parametrize("kind", ["show", "episode"])
def test_rgb_artwork_resize_preserves_proportions(tmp_path, kind):
    from termicast.importer import _convert_import_artwork
    from termicast.validation import inspect_local_artwork

    path = tmp_path / "wide.png"
    Image.new("RGB", (800, 400), (255, 0, 0)).save(path)
    approvals = []

    def approve(url, format_name, mode, size, target_size=None):
        approvals.append((size, target_size))
        return True

    _convert_import_artwork(path, "wide.png", approve, kind=kind)
    assert approvals == [((800, 400), (3000, 3000))]
    inspect_local_artwork(path, episode=kind == "episode")
    with Image.open(path) as image:
        assert image.size == (3000, 3000)
        assert image.getpixel((1500, 0)) == (255, 255, 255)
        assert image.getpixel((1500, 749)) == (255, 255, 255)
        assert image.getpixel((1500, 750)) == (255, 0, 0)
        assert image.getpixel((1500, 2249)) == (255, 0, 0)
        assert image.getpixel((1500, 2250)) == (255, 255, 255)


def test_auto_mode_also_approves_resizing(tmp_path, monkeypatch):
    from termicast import cli
    from termicast.importer import _convert_import_artwork

    prompts = []
    monkeypatch.setattr(cli, "menu", lambda *args: prompts.append(args) or 2)
    reviewer = cli._artwork_reviewer()
    assert reviewer("first.png", "PNG", "RGBA", (3000, 3000))
    path = tmp_path / "small.jpg"
    Image.new("RGB", (800, 800), (255, 0, 0)).save(path)
    _convert_import_artwork(path, "small.jpg", reviewer, kind="episode")
    assert len(prompts) == 1
    with Image.open(path) as image:
        assert (image.mode, image.format, image.size) == ("RGB", "JPEG", (3000, 3000))
