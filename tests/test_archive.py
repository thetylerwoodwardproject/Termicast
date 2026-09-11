import json
from pathlib import Path

from lxml import etree

from termicast.archive import load_manifest, merged_template, import_archive, archive_identity
from termicast.models import new_show


FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
 <channel>
  <title>Archived Show</title>
  <description>Desc</description>
  <link>https://old.example.org/show</link>
  <itunes:author>Author</itunes:author>
  <itunes:explicit>no</itunes:explicit>
  <podcast:guid xmlns:podcast="https://podcastindex.org/namespace/1.0">5eaf7b5e-cc24-5e12-9c73-96c1f81c3b12</podcast:guid>
  <item>
   <title>Episode One</title>
   <guid isPermaLink="false">ep-1</guid>
   <description>First</description>
   <enclosure url="https://old.example.org/audio/ep1.mp3" length="1234" type="audio/mpeg"/>
   <pubDate>Sun, 01 Sep 2024 12:00:00 GMT</pubDate>
   <itunes:duration>60</itunes:duration>
  </item>
 </channel>
</rss>
"""

PAGE2 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
 <channel>
  <item>
   <title>Episode Two</title>
   <guid isPermaLink="false">ep-2</guid>
   <description>Second</description>
   <enclosure url="https://old.example.org/audio/ep2.mp3" length="99" type="audio/mpeg"/>
   <pubDate>Sun, 01 Sep 2024 11:00:00 GMT</pubDate>
   <itunes:duration>30</itunes:duration>
  </item>
 </channel>
</rss>
"""


def make_archive(tmp_path, with_page=False):
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "feed.xml").write_text(FEED, encoding="utf-8")
    assets = {"https://old.example.org/audio/ep1.mp3": "audio/ep1.mp3"}
    (archive / "audio").mkdir()
    (archive / "audio" / "ep1.mp3").write_bytes(b"fake-audio")
    pages = []
    if with_page:
        (archive / "pages").mkdir()
        (archive / "pages" / "page2.xml").write_text(PAGE2, encoding="utf-8")
        pages = ["pages/page2.xml"]
        assets["https://old.example.org/audio/ep2.mp3"] = "audio/ep2.mp3"
        (archive / "audio" / "ep2.mp3").write_bytes(b"fake-audio-2")
    manifest = {"version": 1, "feed": "feed.xml", "pages": pages, "assets": assets}
    (archive / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return archive / "manifest.json"


def test_load_manifest(tmp_path):
    path, data = load_manifest(make_archive(tmp_path))
    assert data["feed"] == "feed.xml"
    assert data["assets"]["https://old.example.org/audio/ep1.mp3"] == "audio/ep1.mp3"


def test_merged_template_gathers_pages(tmp_path):
    path, data = load_manifest(make_archive(tmp_path, with_page=True))
    template = merged_template(path, data)
    root = etree.fromstring(template)
    assert len(root.findall("channel/item")) == 2


def test_archive_identity(tmp_path):
    identity = archive_identity(make_archive(tmp_path))
    assert identity["title"] == "Archived Show"
    assert identity["guid"] == "5eaf7b5e-cc24-5e12-9c73-96c1f81c3b12"


def test_import_archive_installs_without_redownload(tmp_path):
    manifest = make_archive(tmp_path)
    identity = archive_identity(manifest)
    show = new_show(**identity)
    show.update(identity)
    show["output_dir"] = str(tmp_path / "out")
    show["base_url"] = "https://new.example.org/show"
    show, episodes, template = import_archive(show, manifest)
    assert len(episodes) == 1
    assert episodes[0]["guid"] == "ep-1"
    assert episodes[0]["mp3_url"] == "https://new.example.org/show/audio/ep1.mp3"
    assert (tmp_path / "out" / "audio" / "ep1.mp3").is_file()
    assert show["import_url_map"]["https://old.example.org/audio/ep1.mp3"] == "https://new.example.org/show/audio/ep1.mp3"


def test_archive_feed_writes_manifest(tmp_path, monkeypatch):
    from termicast import validation
    from termicast.archive import archive_feed
    feed = tmp_path / "source.xml"
    feed.write_text(FEED, encoding="utf-8")
    dest = tmp_path / "archive"

    def fake_download(url, target, limit, progress=None):
        target.write(b"fake-bytes")

    monkeypatch.setattr(validation, "_download", fake_download)
    manifest_path = archive_feed(str(feed), str(dest))
    data = json.loads(manifest_path.read_text())
    assert data["feed"] == "feed.xml"
    assert data["pages"] == []
    assert "https://old.example.org/audio/ep1.mp3" in data["assets"]
    assert (dest / data["assets"]["https://old.example.org/audio/ep1.mp3"]).is_file()
    assert (dest / "feed.xml").is_file()


def test_restage_previews_mapping(tmp_path):
    from termicast.archive import restage
    manifest = make_archive(tmp_path)
    mapping, errors = restage(manifest, "https://new.example.org/show")
    assert mapping["https://old.example.org/audio/ep1.mp3"] == "https://new.example.org/show/audio/ep1.mp3"
    assert errors == []
