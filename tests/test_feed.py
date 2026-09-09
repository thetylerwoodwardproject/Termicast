from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import NAMESPACE_URL, uuid5

from lxml import etree
import pytest

from termicast.feed import MAX_FEED_BYTES, NS, render_feed, validate_feed
from termicast.importer import _HTTPSRedirectHandler, import_feed


FIXTURE = Path(__file__).parent / "fixtures" / "sample.xml"
NOW = datetime(2026, 9, 8, 12, 30, tzinfo=timezone.utc)


@pytest.fixture
def show():
    profile, _ = import_feed(str(FIXTURE))
    profile["base_url"] = "https://new.example.org/show/"
    return profile


@pytest.fixture
def episode():
    return {
        "guid": "aa2b7474-69f6-4457-8f17-1792d0cd491f", "title": "New & exciting",
        "description": '<p>Hello & <a href="https://example.org/?a=1&b=2">welcome</a></p>',
        "link": "https://example.org/new", "mp3_url": "https://example.org/new.mp3",
        "length": 1234567, "duration": 3670.25, "episode_type": "full",
        "episode_number": 3, "season_number": 2, "explicit": True,
        "artwork_url": "https://example.org/new.jpg", "transcript_url": "https://example.org/new.vtt",
        "keywords": ["sound", "stories"],
        "soundbites": [{"startTime": 1.5, "duration": 20, "title": "A & B"}],
        "chapters": [{"startTime": 0, "endTime": 60.5, "title": "Opening"},
                     {"startTime": 60.5, "endTime": 120, "title": "Part two"}],
        "published_at": "2026-09-08T11:00:00+00:00",
    }


def test_import_settings_and_original_bytes():
    show, data = import_feed(str(FIXTURE))
    assert data == FIXTURE.read_bytes()
    assert json.loads(json.dumps(show)) == show
    assert show["guid"] == "5eaf7b5e-cc24-5e12-9c73-96c1f81c3b12"
    assert show["title"] == "Representative & Friends"
    assert show["owner_email"] == "owner@example.org"
    assert show["locked"] is True and show["explicit"] is False
    assert show["category"] == "Arts" and show["subcategory"] == "Performing Arts"
    assert show["secondary_category"] == "Music"
    assert show["podroll"][0]["title"] == "A Friend"
    assert show["base_url"] == "https://old.example.org/podcast"


def test_new_feed_all_episode_fields(show, episode):
    data = render_feed(show, None, [episode], NOW)
    assert validate_feed(data) == []
    root = etree.fromstring(data)
    assert all(uri in root.nsmap.values() for uri in NS.values())
    channel = root.find("channel")
    assert channel.findtext("generator") == "Termicast"
    assert channel.findtext("lastBuildDate") == "Tue, 08 Sep 2026 12:30:00 GMT"
    assert channel.find("atom:link", NS).get("href") == "https://new.example.org/show/feed.xml"
    assert channel.find("itunes:category/itunes:category", NS).get("text") == "Performing Arts"
    item = channel.find("item")
    assert item.findtext("description") == episode["description"]
    assert item.findtext("content:encoded", namespaces=NS) == episode["description"]
    assert item.find("enclosure").attrib == {"url": episode["mp3_url"], "length": "1234567", "type": "audio/mpeg"}
    assert item.findtext("itunes:duration", namespaces=NS) == "3670.25"
    assert item.findtext("itunes:episode", namespaces=NS) == "3"
    assert item.findtext("itunes:season", namespaces=NS) == "2"
    assert item.findtext("itunes:explicit", namespaces=NS) == "true"
    assert item.findtext("itunes:keywords", namespaces=NS) == "sound,stories"
    assert item.find("podcast:transcript", NS).get("type") == "text/vtt"
    assert item.find("podcast:soundbite", NS).attrib == {"startTime": "1.5", "duration": "20"}
    assert item.find("podcast:chapters", NS).get("url").endswith(f'/chapters/{episode["guid"]}.json')
    assert item.findall("psc:chapters/psc:chapter", NS)[1].get("start") == "00:01:00.500"


def test_import_preserves_extensions_and_unchanged_settings(show, episode):
    template = FIXTURE.read_bytes()
    rendered = etree.fromstring(render_feed(show, template, [episode], NOW))
    original = etree.fromstring(template)
    # Namespace declarations may move; compare element content rather than bytes.
    def signature(element):
        return (element.tag, dict(element.attrib), element.text,
                [signature(child) for child in element])
    for path in ("channel/item", "channel/podcast:value", "channel/podcast:location",
                 "channel/podcast:txt", "channel/itunes:owner", "channel/podcast:locked",
                 "channel/podcast:podroll", "channel/podcast:medium"):
        before = original.find(path, NS)
        after = rendered.findall(path, NS)[-1] if path == "channel/item" else rendered.find(path, NS)
        assert signature(after) == signature(before)
    assert len(rendered.findall("channel/itunes:category", NS)) == 3
    assert len(rendered.findall("channel/podcast:funding", NS)) == 2
    assert rendered.find("channel/{https://example.org/extensions}metadata") is not None
    assert len(rendered.xpath("//comment()")) == 1
    assert rendered.find("channel/atom:link[@rel='hub']", NS) is not None


def test_absent_settings_and_guid_are_not_destructive():
    data = render_feed({"title": "Renamed", "guid": "do-not-replace", "owner_name": "New owner"},
                       FIXTURE.read_bytes(), [], NOW)
    channel = etree.fromstring(data).find("channel")
    assert channel.findtext("title") == "Renamed"
    assert channel.findtext("podcast:guid", namespaces=NS) == "5eaf7b5e-cc24-5e12-9c73-96c1f81c3b12"
    assert channel.findtext("itunes:owner/itunes:email", namespaces=NS) == "owner@example.org"
    assert channel.findtext("itunes:owner/itunes:name", namespaces=NS) == "New owner"
    assert channel.find("itunes:owner/{https://example.org/extensions}ownerNote", NS) is not None
    assert len(channel.findall("itunes:category", NS)) == 3


def test_explicit_clear_optional_settings(show):
    show.update(artwork_url="", funding_url="", podroll=[], category="", subcategory="", secondary_category="")
    channel = etree.fromstring(render_feed(show, FIXTURE.read_bytes(), [], NOW)).find("channel")
    for path in ("image", "itunes:image", "podcast:funding", "podcast:podroll", "itunes:category"):
        assert channel.find(path, NS) is None


def test_episode_deduplication_sorting_and_idempotency(show, episode):
    older = episode | {"title": "Old revision", "published_at": "2026-09-07T11:00:00Z"}
    another = episode | {"guid": "c537731e-b637-45a8-a761-b061620de126", "published_at": "2026-09-08T14:00:00+02:00"}
    episodes = [older, another, episode, episode]
    first = render_feed(show, FIXTURE.read_bytes(), episodes, NOW)
    second = render_feed(show, first, episodes, NOW)
    for data in (first, second):
        items = etree.fromstring(data).findall("channel/item")
        assert [item.findtext("guid") for item in items] == [another["guid"], episode["guid"], "rss-host-original-id-42"]
        assert items[1].findtext("title") == episode["title"]
        assert validate_feed(data) == []


def test_fallback_guid_is_stable_and_uses_initial_url(tmp_path):
    path = tmp_path / "feed.xml"
    path.write_bytes(b'<rss version="2.0"><channel><title>Test</title><atom:link xmlns:atom="http://www.w3.org/2005/Atom" rel="self" href="https://example.org/rss"/></channel></rss>')
    first, _ = import_feed(str(path))
    second, _ = import_feed(str(path))
    assert first["guid"] == second["guid"] == str(uuid5(NAMESPACE_URL, "https://example.org/rss"))
    data = render_feed(first | {"base_url": "https://new.example.org"}, None, [], NOW)
    assert etree.fromstring(data).findtext("channel/podcast:guid", namespaces=NS) == first["guid"]


@pytest.mark.parametrize("data", [b"not XML", b"<rss><channel>", b"<feed/>",
    b"<rss><channel/><channel/></rss>",
    b'<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><rss><channel><title>&xxe;</title></channel></rss>'])
def test_bad_xml_is_rejected(data, tmp_path):
    path = tmp_path / "bad.xml"
    path.write_bytes(data)
    with pytest.raises(ValueError):
        import_feed(str(path))
    assert validate_feed(data)
    with pytest.raises(ValueError):
        render_feed({}, data, [], NOW)


def test_https_import_bounded_and_keeps_initial_identity():
    data = b'<rss version="2.0"><channel><title>Test</title></channel></rss>'
    response = BytesIO(data)
    response.headers = {}
    response.geturl = lambda: "https://redirect.example.org/feed.xml"
    opener = Mock()
    opener.open.return_value = response
    with patch("termicast.importer.build_opener", return_value=opener):
        show, original = import_feed("https://initial.example.org/feed.xml")
    assert original == data
    assert show["guid"] == str(uuid5(NAMESPACE_URL, "https://initial.example.org/feed.xml"))
    assert opener.open.call_args.kwargs["timeout"] == 15


@pytest.mark.parametrize("source", ["http://example.org/feed", "ftp://example.org/feed", "https://user:password@example.org/feed", "https:///feed"])
def test_reject_unsafe_sources(source):
    with pytest.raises(ValueError):
        import_feed(source)


def test_reject_http_redirect():
    with pytest.raises(ValueError):
        _HTTPSRedirectHandler().redirect_request(None, None, 302, "", {}, "http://example.org/feed")


def test_oversized_feed(tmp_path):
    path = tmp_path / "large.xml"
    path.write_bytes(b" " * (MAX_FEED_BYTES + 1))
    with pytest.raises(ValueError, match="size limit"):
        import_feed(str(path))


def test_validation_reports_item_errors(show, episode):
    root = etree.fromstring(render_feed(show, None, [episode], NOW))
    item = root.find("channel/item")
    item.find("enclosure").set("length", "NaN")
    item.find("pubDate").text = "yesterday"
    root.find("channel").append(etree.fromstring(etree.tostring(item)))
    errors = validate_feed(etree.tostring(root))
    assert any("duplicate guid" in error for error in errors)
    assert any("enclosure length" in error for error in errors)
    assert any("pubDate" in error for error in errors)


def test_naive_timestamps_rejected(show):
    with pytest.raises(ValueError, match="timezone-aware"):
        render_feed(show, None, [], datetime(2026, 1, 1))


def test_validation_reports_metadata_limits(show, episode):
    episode.update(title="x" * 61, description="x" * 4001, episode_type="unknown",
                   keywords=[str(i) for i in range(11)], season_number=0)
    root = etree.fromstring(render_feed(show, None, [episode], NOW))
    root.find("channel/item/itunes:duration", NS).text = "NaN"
    errors = validate_feed(etree.tostring(root))
    for expected in ("title exceeds 60", "description exceeds 4000", "episode type",
                     "ten keywords", "positive integer", "duration"):
        assert any(expected in error for error in errors)


@pytest.mark.parametrize("advertised", [True, False])
def test_http_body_limit(advertised):
    response = BytesIO(b" " * (MAX_FEED_BYTES + 1))
    response.headers = {"Content-Length": str(MAX_FEED_BYTES + 1)} if advertised else {}
    response.geturl = lambda: "https://example.org/feed.xml"
    opener = Mock()
    opener.open.return_value = response
    with patch("termicast.importer.build_opener", return_value=opener):
        with pytest.raises(ValueError, match="size limit"):
            import_feed("https://example.org/feed.xml")


def test_partial_funding_update_and_image_title():
    data = render_feed({"title": "Renamed", "funding_url": None, "funding_label": "Donate"},
                       FIXTURE.read_bytes(), [], NOW)
    channel = etree.fromstring(data).find("channel")
    assert channel.findtext("image/title") == "Renamed"
    assert channel.find("podcast:funding", NS).get("url") == "https://example.org/support"
    assert channel.findtext("podcast:funding", namespaces=NS) == "Donate"


def test_optional_website_falls_back_to_public_base_url(show):
    show["website"] = ""
    data = render_feed(show, None, [], datetime.now(timezone.utc))
    root = etree.fromstring(data)
    assert root.findtext("channel/link") == show["base_url"].rstrip("/")
    assert root.findtext("channel/image/link") == show["base_url"].rstrip("/")
    assert validate_feed(data) == []


def test_chapters_url_retains_guid_case(show, episode):
    episode["guid"] = episode["guid"].upper()
    root = etree.fromstring(render_feed(show, None, [episode], NOW))
    url = root.find("channel/item/podcast:chapters", NS).get("url")
    assert url.endswith(f'/chapters/{episode["guid"]}.json')
