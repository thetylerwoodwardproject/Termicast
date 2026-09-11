from uuid import NAMESPACE_URL, uuid4, uuid5

from termicast.models import (
    new_show, new_episode, validate_slug, slug_error, suggest_slug,
    chapters_relative, transcript_relative, chapter_filename,
)


def test_slug_validation():
    assert validate_slug("s02ep042")
    assert validate_slug("episode-42_notes")
    assert validate_slug("a1")
    assert not validate_slug("")
    assert not validate_slug("-leading")
    assert not validate_slug("_leading")
    assert not validate_slug("has space")
    assert not validate_slug("has/slash")
    assert not validate_slug("..")
    assert not validate_slug("s02ep042.mp3")
    assert slug_error("../etc/passwd") is not None


def test_suggest_slug_from_season_and_number():
    episode = new_episode(season_number=2, episode_number=42)
    assert suggest_slug(episode) == "s02ep042"
    episode = new_episode(episode_number=7)
    assert suggest_slug(episode) == "ep007"


def test_suggest_slug_falls_back_to_guid():
    episode = new_episode()
    assert suggest_slug(episode) == episode["guid"]


def test_slug_based_paths_and_legacy_fallback():
    slugged = new_episode(guid=str(uuid4()), slug="s02ep042")
    assert chapters_relative(slugged) == "chapters/s02ep042.json"
    assert transcript_relative(slugged) == "transcripts/s02ep042.vtt"
    legacy = new_episode(guid="rss-host-original-id-42")
    assert chapters_relative(legacy) == "chapters/" + chapter_filename("rss-host-original-id-42")
    assert transcript_relative(legacy).startswith("transcripts/")
    assert transcript_relative(legacy).endswith(".vtt")


def test_show_guid_stable_across_base_url_change():
    show = new_show(base_url="https://a.example.org/podcast")
    guid = show["guid"]
    show["base_url"] = "https://b.example.org/podcast"
    assert show["guid"] == guid
    assert guid == str(uuid5(NAMESPACE_URL, "https://a.example.org/podcast/feed.xml"))


def test_show_defaults():
    show = new_show(base_url="https://a.example.org/podcast")
    assert show["hosting"] == "local"
    assert show["audio_preset"] == "standard"
    assert show["image_preset"] == "compact"
    assert show["enabled"] is False


def test_episode_has_fresh_guid_and_slug_fields():
    episode = new_episode()
    assert episode["guid"]
    assert episode["slug"] == ""
    assert episode["audio_path"] == ""
    assert episode["image_path"] == ""
    assert episode["transcript_path"] == ""
