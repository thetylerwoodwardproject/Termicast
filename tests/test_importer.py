from datetime import datetime, timezone
from pathlib import Path

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
