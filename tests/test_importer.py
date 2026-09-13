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
        converted = _convert_import_artwork(path, str(path), reviewer)
        with Image.open(converted) as image:
            assert image.mode == "RGB"
            assert image.getpixel((0, 0)) == (120, 120, 120)
    assert len(prompts) == 1
    assert cli._artwork_reviewer()("new.png", "PNG", "RGBA", (1400, 1400))
    assert len(prompts) == 2


def test_download_import_checks_s3_before_clearing_previous_import(tmp_path, monkeypatch):
    from termicast import importer, s3deploy
    output = tmp_path / "out"
    output.mkdir()
    (output / "feed.xml").write_text("<old/>")
    show = new_show(title="S", description="d", base_url="https://e.org/show",
                    output_dir=str(output), hosting="s3", bucket="b", prefix="p",
                    asset_base_url="https://cdn.e.org/show")
    cleared = []
    monkeypatch.setattr(importer, "_clear_previous_import", lambda o: cleared.append(o))
    monkeypatch.setattr(s3deploy, "check_s3_destination",
                        lambda s: (_ for _ in ()).throw(RuntimeError("s3 boom")))
    template = b'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel></channel></rss>'
    with pytest.raises(RuntimeError, match="s3 boom"):
        importer.download_import(show, template, overwrite=True)
    assert cleared == []


def test_rgb_jpeg_artwork_is_preserved(tmp_path):
    from termicast.importer import _convert_import_artwork

    path = tmp_path / "rgb.jpg"
    Image.new("RGB", (10, 10)).save(path, "JPEG")
    original = path.read_bytes()
    result = _convert_import_artwork(path, "image.jpg", lambda *args: pytest.fail("Unexpected prompt"))
    assert result == path
    assert path.read_bytes() == original


def test_converted_artwork_still_requires_valid_dimensions(tmp_path):
    from termicast.importer import _convert_import_artwork
    from termicast.validation import inspect_local_artwork

    path = tmp_path / "small.png"
    Image.new("RGBA", (10, 10)).save(path)
    converted = _convert_import_artwork(path, "small.png", lambda *args: True)
    with pytest.raises(ValueError, match="Show artwork must be square"):
        inspect_local_artwork(converted)


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

    converted = _convert_import_artwork(path, "wide.png", approve, kind=kind)
    assert approvals == [((800, 400), (3000, 3000))]
    inspect_local_artwork(converted, episode=kind == "episode")
    with Image.open(converted) as image:
        assert image.size == (3000, 3000)
        # JPEG is lossy, so assert bands rather than exact channel values.
        for coord in ((1500, 0), (1500, 749), (1500, 2250)):
            assert all(channel >= 220 for channel in image.getpixel(coord))
        for coord in ((1500, 750), (1500, 2249)):
            r, g, b = image.getpixel(coord)
            assert r >= 180 and g <= 100 and b <= 100


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


def test_shared_asset_urls_counts_chapter_images_across_episodes():
    """A chapter image reused by several chapters of one episode is still renamed."""
    from termicast.importer import _shared_asset_urls
    episodes = [
        {"guid": "a", "mp3_url": "https://x.example/a.mp3",
         "chapters": [{"img": "https://x.example/c1.jpg"}]},
        {"guid": "b", "mp3_url": "https://x.example/b.mp3",
         "chapters": [{"img": "https://x.example/c1.jpg"},
                      {"img": "https://x.example/c2.jpg"}]},
    ]
    shared = _shared_asset_urls(episodes)
    assert "https://x.example/c1.jpg" in shared
    assert "https://x.example/c2.jpg" not in shared
    assert "https://x.example/a.mp3" not in shared


def test_import_naming_renames_chapter_images(tmp_path):
    """Chapter images follow the episode slug when a naming scheme is applied."""
    from termicast.importer import download_import

    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"'
        ' xmlns:psc="http://podlove.org/simple-chapters">'
        '<channel><title>S</title><description>d</description><link>https://old.example.org</link>'
        '<item><title>Ep</title><guid isPermaLink="false">ep-1</guid><description>d</description>'
        '<enclosure url="https://old.example.org/audio/ep1.mp3" length="1234" type="audio/mpeg"/>'
        '<pubDate>Sun, 01 Sep 2024 12:00:00 GMT</pubDate>'
        '<itunes:duration>60</itunes:duration>'
        '<psc:chapters version="1.2">'
        '<psc:chapter start="00:00:00.000" title="First" image="https://old.example.org/images/c1.jpg"/>'
        '<psc:chapter start="00:00:30.000" title="Second" image="https://old.example.org/images/c2.jpg"/>'
        '</psc:chapters>'
        '</item></channel></rss>'
    ).encode()

    src = tmp_path / "src"
    src.mkdir()
    audio = src / "ep1.mp3"
    audio.write_bytes(b"fake-audio")
    c1 = src / "c1.jpg"
    c2 = src / "c2.jpg"
    Image.new("RGB", (100, 100)).save(c1, "JPEG")
    Image.new("RGB", (120, 120)).save(c2, "JPEG")
    preseed = {
        "https://old.example.org/audio/ep1.mp3": (str(audio), "audio/ep1.mp3"),
        "https://old.example.org/images/c1.jpg": (str(c1), "images/chapters/c1.jpg"),
        "https://old.example.org/images/c2.jpg": (str(c2), "images/chapters/c2.jpg"),
    }

    show = new_show(title="S", description="d", base_url="https://new.example.org/show",
                    output_dir=str(tmp_path / "out"))
    show, episodes = download_import(show, feed, preseed=preseed,
                                     require_preseed=True, naming="ep")

    episode = episodes[0]
    assert episode["slug"] == "ep001"
    assert episode["chapters"][0]["img"] == "https://new.example.org/show/images/chapters/ep001-01.jpg"
    assert episode["chapters"][1]["img"] == "https://new.example.org/show/images/chapters/ep001-02.jpg"
    assert (tmp_path / "out" / "images" / "chapters" / "ep001-01.jpg").is_file()
    assert (tmp_path / "out" / "images" / "chapters" / "ep001-02.jpg").is_file()


def test_import_refuses_to_overwrite_slug_named_chapter_json(tmp_path, monkeypatch):
    """A slug-named chapter file already on disk blocks the import.

    Chapter JSON lands at `chapters/<slug>.json` once a naming scheme is
    applied, so an existing file under that name must stop the import rather
    than be overwritten. Pins the invariant that the pre-import destination
    checks cover the paths the publisher actually writes.
    """
    from termicast import importer, s3deploy

    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"'
        ' xmlns:psc="http://podlove.org/simple-chapters">'
        '<channel><title>S</title><description>d</description><link>https://old.example.org</link>'
        '<item><title>Ep</title><guid isPermaLink="false">ep-1</guid><description>d</description>'
        '<enclosure url="https://old.example.org/audio/ep1.mp3" length="1234" type="audio/mpeg"/>'
        '<pubDate>Sun, 01 Sep 2024 12:00:00 GMT</pubDate>'
        '<itunes:duration>60</itunes:duration>'
        '<psc:chapters version="1.2">'
        '<psc:chapter start="00:00:00.000" title="First"/>'
        '</psc:chapters>'
        '</item></channel></rss>'
    ).encode()

    src = tmp_path / "src"
    src.mkdir()
    audio = src / "ep1.mp3"
    audio.write_bytes(b"fake-audio")

    output = tmp_path / "out"
    (output / "chapters").mkdir(parents=True)
    occupied = output / "chapters" / "ep001.json"
    occupied.write_text("pre-existing", encoding="utf-8")

    monkeypatch.setattr(s3deploy, "check_s3_destination", lambda s: None)
    show = new_show(title="S", description="d", base_url="https://new.example.org/show",
                    output_dir=str(output), hosting="s3", bucket="b", prefix="p",
                    asset_base_url="https://cdn.example.org/show")
    preseed = {"https://old.example.org/audio/ep1.mp3": (str(audio), "audio/ep1.mp3")}

    with pytest.raises(ValueError, match="ep001"):
        importer.download_import(show, feed, preseed=preseed,
                                 require_preseed=True, naming="ep")
    assert occupied.read_text(encoding="utf-8") == "pre-existing"



@pytest.mark.parametrize("suffix,content_type", [
    (".m4a", "audio/mp4"), (".opus", "audio/ogg"), (".flac", "audio/flac"),
    (".bin", "audio/mpeg"),   # unknown suffix still falls back to .mp3
])
def test_downloaded_audio_keeps_deliverable_suffixes(tmp_path, monkeypatch, suffix, content_type):
    """A downloaded non-MP3 enclosure must keep its own suffix.

    The importer carried a narrower suffix list than the archive adapter, so
    an imported .m4a landed as .mp3 and was then served as audio/mpeg -- a
    Content-Type that did not describe the bytes. Only the download path
    renames suffixes; a preseeded asset keeps the name it was staged under.
    """
    from termicast import importer, validation
    from termicast.media import content_type_for

    url = f"https://old.example.org/audio/ep1{suffix}"
    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
        '<channel><title>S</title><description>d</description><link>https://old.example.org</link>'
        '<item><title>Ep</title><guid isPermaLink="false">ep-1</guid><description>d</description>'
        f'<enclosure url="{url}" length="1234" type="audio/mpeg"/>'
        '<pubDate>Sun, 01 Sep 2024 12:00:00 GMT</pubDate>'
        '<itunes:duration>60</itunes:duration>'
        '</item></channel></rss>'
    ).encode()

    monkeypatch.setattr(validation, "_download",
                        lambda u, handle, limit, progress=None: handle.write(b"fake-audio"))
    monkeypatch.setattr(validation, "probe_local_media",
                        lambda path: {"length": 10, "duration": 60.0})

    show = new_show(title="S", description="d", base_url="https://new.example.org/show",
                    output_dir=str(tmp_path / "out"))
    show, episodes = importer.download_import(show, feed)

    staged = episodes[0]["mp3_url"]
    expected = suffix if suffix != ".bin" else ".mp3"
    assert staged.endswith(expected), staged
    assert content_type_for("audio/x" + expected) == content_type


def test_linked_srt_transcript_is_kept_as_subrip(tmp_path, monkeypatch):
    """SubRip is a valid podcast:transcript format, so import keeps it as SubRip."""
    from termicast import importer, validation
    from termicast.media import transcript_type

    transcript = "https://old.example.org/transcripts/ep1.srt"
    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"'
        ' xmlns:podcast="https://podcastindex.org/namespace/1.0">'
        '<channel><title>S</title><description>d</description><link>https://old.example.org</link>'
        '<item><title>Ep</title><guid isPermaLink="false">ep-1</guid><description>d</description>'
        '<enclosure url="https://old.example.org/audio/ep1.mp3" length="1234" type="audio/mpeg"/>'
        f'<podcast:transcript url="{transcript}" type="application/x-subrip"/>'
        '<pubDate>Sun, 01 Sep 2024 12:00:00 GMT</pubDate>'
        '<itunes:duration>60</itunes:duration>'
        '</item></channel></rss>'
    ).encode()

    srt = b"1\r\n00:00:01,000 --> 00:00:04,000\r\nHello\r\n"
    monkeypatch.setattr(validation, "_download", lambda u, handle, limit, progress=None:
                        handle.write(srt if u.endswith(".srt") else b"fake-audio"))
    monkeypatch.setattr(validation, "probe_local_media",
                        lambda path: {"length": 10, "duration": 60.0})

    show = new_show(title="S", description="d", base_url="https://new.example.org/show",
                    output_dir=str(tmp_path / "out"))
    show, episodes = importer.download_import(show, feed)

    staged = episodes[0]["transcript_url"]
    assert staged.endswith(".srt"), staged
    assert transcript_type(staged) == "application/x-subrip"
    relative = staged.split("/show/", 1)[1]
    assert (tmp_path / "out" / relative).read_bytes() == srt


def test_linked_srt_transcript_without_cues_is_rejected(tmp_path, monkeypatch):
    from termicast import importer, validation

    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"'
        ' xmlns:podcast="https://podcastindex.org/namespace/1.0">'
        '<channel><title>S</title><description>d</description><link>https://old.example.org</link>'
        '<item><title>Ep</title><guid isPermaLink="false">ep-1</guid><description>d</description>'
        '<enclosure url="https://old.example.org/audio/ep1.mp3" length="1234" type="audio/mpeg"/>'
        '<podcast:transcript url="https://old.example.org/transcripts/ep1.srt" type="application/x-subrip"/>'
        '<pubDate>Sun, 01 Sep 2024 12:00:00 GMT</pubDate>'
        '<itunes:duration>60</itunes:duration>'
        '</item></channel></rss>'
    ).encode()

    monkeypatch.setattr(validation, "_download", lambda u, handle, limit, progress=None:
                        handle.write(b"<html>not a transcript</html>" if u.endswith(".srt") else b"fake-audio"))
    monkeypatch.setattr(validation, "probe_local_media",
                        lambda path: {"length": 10, "duration": 60.0})

    show = new_show(title="S", description="d", base_url="https://new.example.org/show",
                    output_dir=str(tmp_path / "out"))
    with pytest.raises(ValueError, match="SubRip"):
        importer.download_import(show, feed)
