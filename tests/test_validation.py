from datetime import datetime, timezone
from io import BytesIO
import json
import subprocess
from types import SimpleNamespace
from urllib.request import Request
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from termicast.models import new_episode, new_show
from termicast import validation as v


@pytest.fixture
def show():
    return new_show(title="Show", description="Description", base_url="https://example.com/podcast/",
                    output_dir="/tmp/podcast", category="Arts", subcategory="Books")


@pytest.fixture
def episode():
    return new_episode(title="Episode", description="<p>Hello</p>",
                       mp3_url="https://example.com/audio.mp3", length=123, duration=180)


def test_models(show, episode):
    assert v.validate_show(show) == []
    assert v.validate_episode(episode) == []
    assert UUID(show["id"]).version == UUID(episode["guid"]).version == 4
    assert show["guid"] == str(uuid5(NAMESPACE_URL, "https://example.com/podcast/feed.xml"))
    assert new_show(base_url="https://example.com/podcast")["guid"] == show["guid"]
    guid = show["guid"]
    show["base_url"] = "https://new.example.com"
    assert show["guid"] == guid
    assert new_show(**show) == show
    assert new_episode(**episode) == episode
    assert json.loads(json.dumps(show)) == show
    assert json.loads(json.dumps(episode)) == episode
    for field in ("keywords", "soundbites", "chapters"):
        episode[field].append({})
        assert new_episode()[field] == []
    show["podroll"].append({})
    assert new_show()["podroll"] == []


@pytest.mark.parametrize("url", ["https://example.com", "https://example.com:443/a?q=1#x", "https://[::1]/a"])
def test_https_valid(url):
    assert v.validate_https(url)


@pytest.mark.parametrize("url", [None, 123, "", "http://example.com", "//example.com", "https:///a",
                                    "https://user:pass@example.com", "https://example.com:99999",
                                    "https://example.com:bad", "https://example.com/a\n", "https://exa mple.com",
                                    "https://example.com\\@evil.com", "https://[broken"])
def test_https_invalid(url):
    assert not v.validate_https(url)


@pytest.mark.parametrize("text,seconds", [("0", 0), ("1.25", 1.25), ("02:03.5", 123.5),
                                            ("1:02:03", 3723), ("90:00", 5400), (" 3 ", 3)])
def test_parse_time(text, seconds):
    assert v.parse_time(text) == seconds


@pytest.mark.parametrize("text", ["", "-1", "nan", "inf", "1e2", "1:60", "1:60:00", "1.2:00",
                                     "1::2", "1:2:3:4", "a", None, "9" * 400])
def test_parse_time_invalid(text):
    with pytest.raises(ValueError):
        v.parse_time(text)


def test_local_time():
    assert v.local_to_utc("2026-01-01T12:00", "America/New_York") == datetime(2026, 1, 1, 17, tzinfo=timezone.utc)
    assert v.local_to_utc("2026-11-01T01:30-04:00", "America/New_York").hour == 5
    assert v.local_to_utc("2026-11-01T01:30-05:00", "America/New_York").hour == 6
    assert v.local_to_utc("2026-01-01T00:00Z", "UTC").tzinfo is timezone.utc


@pytest.mark.parametrize("text,zone", [("2026-03-08T02:30", "America/New_York"),
    ("2026-03-08T02:30-05:00", "America/New_York"), ("2026-11-01T01:30", "America/New_York"),
    ("2026-11-01T01:30-06:00", "America/New_York"), ("2026-01-01T12:00Z", "America/New_York"),
    ("garbage", "UTC"), ("2026-01-01", "Invalid/Zone"), (None, "UTC"),
    ("2026-10-04T02:15", "Australia/Lord_Howe"), ("2026-04-05T01:45", "Australia/Lord_Howe")])
def test_local_time_invalid(text, zone):
    with pytest.raises(ValueError):
        v.local_to_utc(text, zone)


@pytest.mark.parametrize("field,value", [("title", "x" * 61), ("description", "<b>" + "x" * 3998),
    ("guid", ""), ("episode_type", "other"), ("episode_number", 0), ("season_number", -1),
    ("episode_number", True), ("season_number", 1.5), ("length", 0), ("length", 1.5),
    ("duration", float("nan")), ("duration", float("inf")), ("duration", 0), ("duration", True),
    ("duration", 10 ** 400), ("keywords", ["x"] * 11), ("keywords", "abc"), ("keywords", [None]),
    ("explicit", "false"), ("title", None), ("mp3_url", ""), ("soundbites", {}), ("chapters", [None])])
def test_episode_invalid(episode, field, value):
    episode[field] = value
    assert any(field in error for error in v.validate_episode(episode))


@pytest.mark.parametrize("field", ["link", "mp3_url", "artwork_url", "transcript_url"])
def test_episode_urls(episode, field):
    episode[field] = "http://example.com/file"
    assert any(field in error for error in v.validate_episode(episode))


def test_episode_boundaries(episode):
    episode.update(title="x" * 60, description="x" * 4000, keywords=["x"] * 10,
                   episode_number=1, season_number=1, episode_type="trailer")
    assert v.validate_episode(episode) == []
    episode["chapters"] = [{"startTime": 0, "endTime": 90, "title": "One"},
                           {"startTime": 90, "endTime": 180, "title": "Two"}]
    episode["soundbites"] = [{"startTime": 0, "duration": 15, "title": "x" * 128},
                             {"startTime": 60, "duration": 120, "stop": 180}]
    assert v.validate_episode(episode) == []


@pytest.mark.parametrize("entry", [{"startTime": -1, "duration": 15}, {"startTime": 1, "duration": 0},
    {"startTime": 170, "duration": 15}, {"startTime": 0, "duration": 15, "title": "x" * 129},
    {"startTime": 0, "duration": 15, "stop": 16}, {"startTime": float("nan"), "duration": 1}])
def test_soundbite_invalid(episode, entry):
    episode["soundbites"] = [entry]
    assert v.validate_episode(episode)


@pytest.mark.parametrize("entries", [[{"startTime": 0, "endTime": 0}], [{"startTime": 0, "endTime": 181}],
    [{"startTime": 0, "endTime": 100}, {"startTime": 99, "endTime": 150}],
    [{"startTime": 100, "endTime": 150}, {"startTime": 0, "endTime": 50}],
    [{"startTime": 0, "endTime": float("inf")}], [{"startTime": 0}]])
def test_chapter_invalid(episode, entries):
    episode["chapters"] = entries
    assert v.validate_episode(episode)


def test_soundbite_order(episode):
    episode["soundbites"] = [{"startTime": 30, "duration": 15}, {"startTime": 0, "duration": 15}]
    assert any("sorted" in error for error in v.validate_episode(episode))


@pytest.mark.parametrize("field,value", [("id", "bad"), ("guid", "bad"), ("title", ""),
    ("description", "x" * 4001), ("owner_email", "bad"), ("language", "bad language"),
    ("timezone", "Invalid/Zone"), ("podcast_type", "full"), ("locked", "yes"),
    ("category", "Invalid"), ("subcategory", "Physics"), ("secondary_category", "Books"),
    ("funding_label", "Donate"), ("podroll", [{}]), ("podroll", [None]), ("podroll", {}),
    ("output_dir", "")])
def test_show_invalid(show, field, value):
    show[field] = value
    assert v.validate_show(show)


@pytest.mark.parametrize("suffix", ["?token=test", "#section"])
def test_base_url_cannot_have_query_or_fragment(show, suffix):
    show["base_url"] += suffix
    assert any("base_url" in error for error in v.validate_show(show))


def test_podroll(show):
    entry = {"feedGuid": new_episode()["guid"], "feedUrl": "https://example.com/feed.xml", "title": "Other"}
    show["podroll"] = [entry.copy() for _ in range(8)]
    assert v.validate_show(show) == []
    show["podroll"].append(entry.copy())
    assert any("8" in error for error in v.validate_show(show))
    show["podroll"] = [{"feedGuid": entry["feedGuid"]}]
    assert v.validate_show(show) == []
    show["podroll"][0]["feedUrl"] = "http://example.com"
    assert any("feedUrl" in error for error in v.validate_show(show))


def test_taxonomy():
    assert len(v.CATEGORIES) == 19
    assert sum(map(len, v.CATEGORIES.values())) == 91
    assert "Mental Health" in v.CATEGORIES["Health & Fitness"]
    assert v.CATEGORIES["True Crime"] == []


class Response(BytesIO):
    def __init__(self, data=b""):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def geturl(self):
        return "https://example.com/file"


def test_probe(monkeypatch):
    monkeypatch.setattr(v, "_open", lambda *args: Response(b"audio"))
    def run(args, **kwargs):
        assert "https://example.com/file" not in args
        assert args[args.index("-protocol_whitelist") + 1] == "file,pipe"
        assert kwargs["timeout"] == v.NETWORK_TIMEOUT
        return SimpleNamespace(stdout='{"format":{"duration":"12.5"}}')
    monkeypatch.setattr(v.subprocess, "run", run)
    assert v.probe_media("https://example.com/file") == {"length": 5, "duration": 12.5}


@pytest.mark.parametrize("failure", [FileNotFoundError(), subprocess.TimeoutExpired("ffprobe", 10), ValueError()])
def test_probe_manual_fallback(monkeypatch, failure):
    monkeypatch.setattr(v, "_open", lambda *args: Response(b"audio"))
    def run(*args, **kwargs):
        raise failure
    monkeypatch.setattr(v.subprocess, "run", run)
    assert v.probe_media("https://example.com/file") == {"length": 5}


def test_network_unavailable(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("offline")
    monkeypatch.setattr(v, "_open", unavailable)
    assert v.probe_media("https://example.com/file") == {}
    assert "unavailable" in v.inspect_artwork("https://example.com/art")[0]


def test_large_media_metadata_only(monkeypatch):
    def open_response(url, method="GET"):
        assert method == "HEAD"
        response = Response()
        response.headers = {"Content-Length": str(v.MAX_MEDIA_BYTES + 1)}
        return response
    monkeypatch.setattr(v, "_open", open_response)
    assert v.probe_media("https://example.com/file") == {"length": v.MAX_MEDIA_BYTES + 1}


def test_head_failure_still_probes(monkeypatch):
    def open_response(url, method="GET"):
        if method == "HEAD":
            raise OSError("HEAD not supported")
        return Response(b"audio")
    monkeypatch.setattr(v, "_open", open_response)
    monkeypatch.setattr(v.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout='{"format":{"duration":"3"}}'))
    assert v.probe_media("https://example.com/file") == {"length": 5, "duration": 3.0}


def test_open_timeout_and_response_url(monkeypatch):
    response = Response()
    def open_response(req, timeout):
        assert req.get_method() == "HEAD"
        assert timeout == v.NETWORK_TIMEOUT
        return response
    def opener(handler):
        assert isinstance(handler, v._HTTPSRedirectHandler)
        return SimpleNamespace(open=open_response)
    monkeypatch.setattr(v.request, "build_opener", opener)
    assert v._open("https://example.com/file", "HEAD") is response
    response.geturl = lambda: "http://example.com/file"
    with pytest.raises(ValueError, match="HTTPS"):
        v._open("https://example.com/file", "HEAD")
    assert response.closed


@pytest.mark.parametrize("helper", [v.probe_media, v.inspect_artwork])
def test_remote_requires_https(helper):
    with pytest.raises(ValueError, match="HTTPS"):
        helper("http://example.com/file")


def test_redirects():
    handler = v._HTTPSRedirectHandler()
    req = Request("https://example.com/file")
    with pytest.raises(ValueError, match="HTTPS"):
        handler.redirect_request(req, None, 302, "Found", {}, "http://example.com/file")
    redirected = handler.redirect_request(req, None, 302, "Found", {}, "https://other.example.com/file")
    assert redirected.full_url == "https://other.example.com/file"


def test_download_bound(monkeypatch):
    monkeypatch.setattr(v, "_open", lambda *args: Response(b"123456"))
    with pytest.raises(ValueError, match="size limit"):
        v._download("https://example.com", BytesIO(), 5)
    target = BytesIO()
    v._download("https://example.com", target, 6)
    assert target.getvalue() == b"123456"


def test_download_deadline(monkeypatch):
    monkeypatch.setattr(v, "_open", lambda *args: Response(b"123"))
    times = iter([0, v.DOWNLOAD_TIMEOUT + 1])
    monkeypatch.setattr(v.time, "monotonic", lambda: next(times))
    with pytest.raises(TimeoutError):
        v._download("https://example.com", BytesIO(), 100)


@pytest.mark.parametrize("size,episode,valid", [((1400, 1400), False, True), ((3000, 3000), False, True),
    ((3000, 3000), True, True), ((1400, 1400), True, False), ((1399, 1399), False, False),
    ((3001, 3001), False, False), ((1500, 1600), False, False)])
def test_artwork(monkeypatch, size, episode, valid):
    Image = pytest.importorskip("PIL.Image")
    content = BytesIO()
    Image.new("RGB", size).save(content, format="PNG")
    monkeypatch.setattr(v, "_open", lambda *args: Response(content.getvalue()))
    if valid:
        assert v.inspect_artwork("https://example.com/art", episode=episode) == []
    else:
        with pytest.raises(ValueError, match="pixels"):
            v.inspect_artwork("https://example.com/art", episode=episode)


def test_corrupt_artwork(monkeypatch):
    monkeypatch.setattr(v, "_open", lambda *args: Response(b"not an image"))
    assert v.inspect_artwork("https://example.com/art")
