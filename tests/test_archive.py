import json
import os

import pytest
from PIL import Image
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


def test_import_archive_stages_inside_a_preexisting_output_dir(tmp_path):
    """A pre-created, writable output directory needs no write access to its
    parent (e.g. a shared, root-owned web root the deploy account can't touch).
    """
    manifest = make_archive(tmp_path)
    identity = archive_identity(manifest)
    show = new_show(**identity)
    show.update(identity)
    out = tmp_path / "webroot" / "out"
    out.parent.mkdir()
    out.mkdir()
    show["output_dir"] = str(out)
    show["base_url"] = "https://new.example.org/show"
    os.chmod(out.parent, 0o555)
    try:
        show, episodes, template = import_archive(show, manifest)
    finally:
        os.chmod(out.parent, 0o755)
    assert (out / "audio" / "ep1.mp3").is_file()


def test_import_archive_refuses_an_existing_feed(tmp_path):
    manifest = make_archive(tmp_path)
    identity = archive_identity(manifest)
    show = new_show(**identity)
    show.update(identity)
    show["output_dir"] = str(tmp_path / "out")
    show["base_url"] = "https://new.example.org/show"
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "feed.xml").write_text("stale")
    with pytest.raises(ValueError, match="already exists"):
        import_archive(show, manifest)


def test_import_archive_overwrite_leaves_unrelated_files_alone(tmp_path):
    """Overwrite must not delete files it didn't create, e.g. a real images/ folder."""
    manifest = make_archive(tmp_path)
    identity = archive_identity(manifest)
    show = new_show(**identity)
    show.update(identity)
    show["output_dir"] = str(tmp_path / "out")
    show["base_url"] = "https://new.example.org/show"
    out = tmp_path / "out"
    (out / "images").mkdir(parents=True)
    (out / "feed.xml").write_text("stale")
    (out / "images" / "unrelated.jpg").write_bytes(b"not ours")
    show, episodes, template = import_archive(show, manifest, overwrite=True)
    assert len(episodes) == 1
    assert (out / "audio" / "ep1.mp3").is_file()
    assert (out / "images" / "unrelated.jpg").is_file()


def test_import_archive_overwrite_reports_a_genuine_collision(tmp_path):
    """Re-importing the same feed collides on the same content-hash filename."""
    manifest = make_archive(tmp_path)
    identity = archive_identity(manifest)
    show = new_show(**identity)
    show.update(identity)
    show["output_dir"] = str(tmp_path / "out")
    show["base_url"] = "https://new.example.org/show"
    import_archive(show, manifest)
    with pytest.raises(ValueError, match="audio/ep1.mp3"):
        import_archive(show, manifest, overwrite=True)


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


@pytest.mark.parametrize("field,value", [
    ("feed", "/etc/passwd"),
    ("feed", "../../../etc/passwd"),
    ("pages", ["../escape.xml"]),
    ("assets", {"https://old.example.org/audio/ep1.mp3": "../../../tmp/evil.mp3"}),
    ("assets", {"https://old.example.org/audio/ep1.mp3": "/tmp/evil.mp3"}),
])
def test_load_manifest_rejects_path_traversal(tmp_path, field, value):
    manifest = make_archive(tmp_path)
    data = json.loads(manifest.read_text())
    data[field] = value
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="relative path|components"):
        load_manifest(manifest)


def test_restage_previews_mapping(tmp_path):
    from termicast.archive import restage
    manifest = make_archive(tmp_path)
    mapping, errors = restage(manifest, "https://new.example.org/show")
    assert mapping["https://old.example.org/audio/ep1.mp3"] == "https://new.example.org/show/audio/ep1.mp3"
    assert errors == []


@pytest.mark.parametrize("source_kind", ["archive", "feed"])
@pytest.mark.parametrize("action", [1, 2, 3])
@pytest.mark.parametrize("artwork_kind", ["show", "episode"])
def test_import_artwork_conversion(tmp_path, monkeypatch, source_kind, action, artwork_kind):
    from termicast import cli, validation
    from termicast.importer import download_import

    manifest = make_archive(tmp_path)
    source = manifest.parent / "cover.png"
    Image.new("RGBA", (1400, 1400), (255, 0, 0, 0)).save(source)
    original = source.read_bytes()
    url = "https://old.example.org/cover.png"
    tag = "channel" if artwork_kind == "show" else "item"
    feed = FEED.replace(f"<{tag}>", f'<{tag}><itunes:image href="{url}"/>')
    (manifest.parent / "feed.xml").write_text(feed)
    data = json.loads(manifest.read_text())
    data["assets"][url] = "cover.png"
    manifest.write_text(json.dumps(data))
    identity = archive_identity(manifest)
    show = new_show(**identity)
    show.update(identity, output_dir=str(tmp_path / "out"), base_url="https://new.example.org/show")
    prompts = []

    def menu(*args):
        prompts.append(args)
        return action

    monkeypatch.setattr(cli, "menu", menu)
    reviewer = cli._artwork_reviewer()

    def download(url, target, limit):
        target.write(original if url.endswith(".png") else b"fake-audio")

    monkeypatch.setattr(validation, "_download", download)

    def run():
        if source_kind == "archive":
            return import_archive(show, manifest, review_artwork=reviewer)
        return download_import(show, feed.encode(), review_artwork=reviewer)

    if action == 3:
        with pytest.raises(RuntimeError, match="Import cancelled"):
            run()
        assert not (tmp_path / "out").exists()
    else:
        run()
        images = list((tmp_path / "out").rglob("*.png"))
        assert len(images) == 1
        with Image.open(images[0]) as image:
            expected_size = (1400, 1400) if artwork_kind == "show" else (3000, 3000)
            assert (image.mode, image.format, image.size) == ("RGB", "PNG", expected_size)
            assert image.getpixel((0, 0)) == (255, 255, 255)
    assert source.read_bytes() == original
    assert len(prompts) == 1
