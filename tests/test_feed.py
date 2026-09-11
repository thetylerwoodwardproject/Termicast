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
