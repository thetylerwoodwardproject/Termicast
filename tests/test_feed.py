from datetime import datetime, timezone

from lxml import etree

from termicast.feed import render_feed, validate_feed, NS, _tag, enclosure_type
from termicast.models import new_show, new_episode


def render(show, episodes):
    return render_feed(show, None, episodes, datetime.now(timezone.utc))


def test_enclosure_type():
    assert enclosure_type("https://e.org/audio/x.mp3") == "audio/mpeg"
    assert enclosure_type("https://e.org/audio/x.m4a") == "audio/mp4"
    assert enclosure_type("https://e.org/audio/x.flac") == "audio/flac"
    assert enclosure_type("https://e.org/audio/x.weird") == "audio/mpeg"


def test_slug_chapters_and_no_psc():
    show = new_show(title="S", description="d", base_url="https://e.org/show", output_dir="/tmp/x")
    episode = new_episode(
        title="Ep", description="desc", guid="00000000-0000-0000-0000-000000000001",
        mp3_url="https://e.org/show/audio/s02ep042.mp3", length=1234, duration=60.0,
        slug="s02ep042", published_at="2024-01-01T00:00:00+00:00",
        chapters=[{"startTime": 0, "endTime": 60, "title": "Opening"}],
    )
    data = render(show, [episode])
    root = etree.fromstring(data)
    chapters = root.find(f"channel/item/{_tag('podcast:chapters')}", NS)
    assert chapters.get("url") == "https://e.org/show/chapters/s02ep042.json"
    assert chapters.get("type") == "application/json+chapters"
    assert root.find(f"channel/item/{_tag('psc:chapters')}", NS) is None


def test_enclosure_type_reflects_format():
    show = new_show(title="S", description="d", base_url="https://e.org/show", output_dir="/tmp/x")
    episode = new_episode(
        title="Ep", description="desc", guid="00000000-0000-0000-0000-000000000002",
        mp3_url="https://e.org/show/audio/e1.m4a", length=1234, duration=60.0,
        published_at="2024-01-01T00:00:00+00:00",
    )
    data = render(show, [episode])
    enclosure = etree.fromstring(data).find("channel/item/enclosure")
    assert enclosure.get("type") == "audio/mp4"


def test_rendered_feed_validates():
    show = new_show(title="S", description="d", base_url="https://e.org/show", output_dir="/tmp/x")
    episode = new_episode(
        title="Ep", description="desc", guid="00000000-0000-0000-0000-000000000003",
        mp3_url="https://e.org/show/audio/e1.mp3", length=1234, duration=60.0,
        published_at="2024-01-01T00:00:00+00:00",
    )
    data = render(show, [episode])
    assert validate_feed(data) == []


def test_op3_prefixes_enclosure():
    show = new_show(title="S", description="d", base_url="https://e.org/show", output_dir="/tmp/x", op3=True)
    episode = new_episode(
        title="Ep", description="desc", guid="00000000-0000-0000-0000-000000000004",
        mp3_url="https://e.org/show/audio/e1.mp3", length=1234, duration=60.0,
        published_at="2024-01-01T00:00:00+00:00",
    )
    data = render(show, [episode])
    enclosure = etree.fromstring(data).find("channel/item/enclosure")
    assert enclosure.get("url") == "https://op3.dev/e/https://e.org/show/audio/e1.mp3"
    assert enclosure.get("type") == "audio/mpeg"
    assert validate_feed(data) == []


def test_op3_off_leaves_enclosure_unprefixed():
    show = new_show(title="S", description="d", base_url="https://e.org/show", output_dir="/tmp/x", op3=False)
    episode = new_episode(
        title="Ep", description="desc", guid="00000000-0000-0000-0000-000000000005",
        mp3_url="https://e.org/show/audio/e1.mp3", length=1234, duration=60.0,
        published_at="2024-01-01T00:00:00+00:00",
    )
    enclosure = etree.fromstring(render(show, [episode])).find("channel/item/enclosure")
    assert enclosure.get("url") == "https://e.org/show/audio/e1.mp3"


def test_op3_prefixes_rewritten_template_enclosure():
    template = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
        '<channel><title>S</title><item>'
        '<title>Ep</title><guid isPermaLink="false">00000000-0000-0000-0000-000000000006</guid>'
        '<enclosure url="https://op3.dev/e/https://old.example.org/audio/e1.mp3" length="1234" type="audio/mpeg"/>'
        '<pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate><itunes:duration>60</itunes:duration>'
        '</item></channel></rss>'
    ).encode()
    show = new_show(title="S", description="d", base_url="https://e.org/show", output_dir="/tmp/x", op3=True)
    show["import_url_map"] = {
        "https://op3.dev/e/https://old.example.org/audio/e1.mp3": "https://new.example.org/audio/e1.mp3",
    }
    episode = new_episode(
        title="Ep", description="desc", guid="00000000-0000-0000-0000-000000000006",
        mp3_url="https://new.example.org/audio/e1.mp3", length=1234, duration=60.0,
        published_at="2024-01-01T00:00:00+00:00",
    )
    data = render_feed(show, template, [episode], datetime.now(timezone.utc))
    enclosure = etree.fromstring(data).find("channel/item/enclosure")
    assert enclosure.get("url") == "https://op3.dev/e/https://new.example.org/audio/e1.mp3"
