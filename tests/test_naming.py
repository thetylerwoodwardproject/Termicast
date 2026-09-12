"""Sequential, human-readable asset names for imported episodes."""

import json

import pytest

from termicast.archive import archive_identity, import_archive
from termicast.models import new_show, plan_slugs, scheme_slug, suggest_slug

from test_archive import make_archive


def imported(tmp_path, **kwargs):
    manifest = make_archive(tmp_path, with_page=True)
    identity = archive_identity(manifest)
    show = new_show(**identity)
    show.update(identity)
    show["output_dir"] = str(tmp_path / "out")
    show["base_url"] = "https://new.example.org/show"
    return import_archive(show, manifest, **kwargs)


# --- naming helpers -------------------------------------------------------

def test_scheme_slug_formats():
    assert scheme_slug({"episode_number": 3}, "ep") == "ep003"
    assert scheme_slug({"episode_number": 3, "season_number": 2}, "ep") == "ep003"
    assert scheme_slug({"episode_number": 3, "season_number": 2}, "sep") == "s02ep003"
    # A show asked for the season style stays consistent when a season is absent.
    assert scheme_slug({"episode_number": 3}, "sep") == "s01ep003"
    assert scheme_slug({"episode_number": 1200}, "ep") == "ep1200"


def test_scheme_slug_matches_suggest_slug():
    # suggest_slug and the schemes share one formatter; pin them together.
    episode = {"episode_number": 42, "season_number": 2}
    assert scheme_slug(episode, "sep") == suggest_slug(episode) == "s02ep042"
    assert scheme_slug({"episode_number": 42}, "ep") == suggest_slug({"episode_number": 42})


def test_scheme_slug_fallbacks():
    assert scheme_slug({}, "ep", position=7) == "ep007"
    assert scheme_slug({}, "ep", position=7, fallback="keep") == ""
    assert scheme_slug({}, "ep") == ""


def test_scheme_slug_rejects_unknown_scheme():
    with pytest.raises(ValueError, match="Naming scheme"):
        scheme_slug({"episode_number": 1}, "title")


def test_plan_slugs_numbers_oldest_first():
    episodes = [
        {"guid": "newer", "published_at": "2024-02-01T00:00:00+00:00"},
        {"guid": "older", "published_at": "2024-01-01T00:00:00+00:00"},
    ]
    assert plan_slugs(episodes, "ep") == {"older": "ep001", "newer": "ep002"}


def test_plan_slugs_keep_fallback_skips_unnumbered():
    episodes = [{"guid": "a", "published_at": "2024-01-01T00:00:00+00:00"},
                {"guid": "b", "episode_number": 4, "published_at": "2024-02-01T00:00:00+00:00"}]
    assert plan_slugs(episodes, "ep", "keep") == {"b": "ep004"}


def test_plan_slugs_rejects_duplicate_names():
    episodes = [{"guid": "a", "episode_number": 1, "season_number": 1},
                {"guid": "b", "episode_number": 1, "season_number": 2}]
    with pytest.raises(ValueError, match="named the same"):
        plan_slugs(episodes, "ep")
    # The season-aware scheme separates them.
    assert plan_slugs(episodes, "sep") == {"a": "s01ep001", "b": "s02ep001"}


# --- import-time naming ---------------------------------------------------

def test_import_without_naming_keeps_archive_names(tmp_path):
    show, episodes, _ = imported(tmp_path)
    assert (tmp_path / "out" / "audio" / "ep1.mp3").is_file()
    assert all(e["slug"] == "" for e in episodes)
    assert all(e["audio_path"] == "" for e in episodes)


def test_import_names_episodes_oldest_first(tmp_path):
    # ep-2 is published at 11:00 and ep-1 at 12:00, so ep-2 is the older one.
    show, episodes, _ = imported(tmp_path, naming="ep")
    by_guid = {e["guid"]: e for e in episodes}
    assert by_guid["ep-2"]["slug"] == "ep001"
    assert by_guid["ep-1"]["slug"] == "ep002"
    assert (tmp_path / "out" / "audio" / "ep001.mp3").is_file()
    assert (tmp_path / "out" / "audio" / "ep002.mp3").is_file()


def test_import_naming_sets_urls_and_paths(tmp_path):
    show, episodes, _ = imported(tmp_path, naming="ep")
    episode = {e["guid"]: e for e in episodes}["ep-2"]
    assert episode["mp3_url"] == "https://new.example.org/show/audio/ep001.mp3"
    assert episode["audio_path"] == "audio/ep001.mp3"


def test_import_naming_rewrites_url_map_in_place(tmp_path):
    show, _, _ = imported(tmp_path, naming="ep")
    mapping = show["import_url_map"]
    # Keyed by the original URL, valued at the new one -- one hop, never two.
    assert mapping["https://old.example.org/audio/ep2.mp3"] == \
        "https://new.example.org/show/audio/ep001.mp3"
    assert not any(value.endswith("/audio/ep2.mp3") for value in mapping.values())


def test_import_naming_sep_scheme(tmp_path):
    show, episodes, _ = imported(tmp_path, naming="sep")
    assert {e["slug"] for e in episodes} == {"s01ep001", "s01ep002"}


def test_import_naming_keep_fallback_leaves_names_alone(tmp_path):
    show, episodes, _ = imported(tmp_path, naming="ep", naming_fallback="keep")
    assert all(e["slug"] == "" for e in episodes)
    assert (tmp_path / "out" / "audio" / "ep1.mp3").is_file()


def test_renamed_feed_points_at_the_new_files(tmp_path):
    from datetime import datetime, timezone
    from termicast.feed import render_feed, validate_feed
    show, episodes, template = imported(tmp_path, naming="ep")
    rendered = render_feed(show, template, episodes, datetime.now(timezone.utc))
    assert b"/audio/ep001.mp3" in rendered
    assert b"/audio/ep2.mp3" not in rendered
    assert validate_feed(rendered) == []


def test_import_refuses_when_the_name_is_taken(tmp_path):
    out = tmp_path / "out"
    (out / "audio").mkdir(parents=True)
    (out / "audio" / "ep001.mp3").write_bytes(b"already here")
    with pytest.raises(ValueError, match="already taken"):
        imported(tmp_path, naming="ep")


def test_name_collision_error_groups_by_slug_and_truncates(tmp_path):
    from termicast.importer import _check_name_destinations
    assets = tmp_path / "assets"
    (assets / "audio").mkdir(parents=True)
    (assets / "images" / "episodes").mkdir(parents=True)
    (assets / "transcripts").mkdir(parents=True)
    slugs = [f"ep{i:03d}" for i in range(1, 45)]
    for slug in slugs:
        (assets / "audio" / f"{slug}.mp3").write_bytes(b"x")
        (assets / "images" / "episodes" / f"{slug}.jpg").write_bytes(b"x")
        (assets / "transcripts" / f"{slug}.vtt").write_bytes(b"x")
    with pytest.raises(ValueError) as excinfo:
        _check_name_destinations(assets, slugs)
    message = str(excinfo.value)
    assert message.startswith("44 episode name(s) already taken")
    assert message.count(".mp3") == 5
    assert "and 39 more" in message
    assert "ep001 (audio/ep001.mp3, images/episodes/ep001.jpg, transcripts/ep001.vtt)" in message


# --- the import-time prompt ----------------------------------------------

def _menu_sequence(monkeypatch, answers):
    from termicast import cli
    steps = iter(answers)
    monkeypatch.setattr(cli, "menu", lambda *a, **k: next(steps))
    return cli


EPISODES = [{"guid": "a", "episode_number": 1, "published_at": "2024-01-01T00:00:00+00:00"},
            {"guid": "b", "episode_number": 2, "published_at": "2024-02-01T00:00:00+00:00"}]


def test_choose_naming_keeps_names(monkeypatch):
    cli = _menu_sequence(monkeypatch, [1])
    assert cli._choose_naming(EPISODES, {}) == (None, "position")


def test_choose_naming_picks_a_scheme(monkeypatch):
    cli = _menu_sequence(monkeypatch, [2])
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: True)
    assert cli._choose_naming(EPISODES, {}) == ("ep", "position")


def test_choose_naming_asks_about_unnumbered_episodes(monkeypatch):
    cli = _menu_sequence(monkeypatch, [3, 2])  # sep scheme, then "keep their names"
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: True)
    episodes = EPISODES + [{"guid": "c", "published_at": "2024-03-01T00:00:00+00:00"}]
    assert cli._choose_naming(episodes, {}) == ("sep", "keep")


def test_choose_naming_declined_keeps_names(monkeypatch):
    cli = _menu_sequence(monkeypatch, [2])
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: False)
    assert cli._choose_naming(EPISODES, {}) == (None, "position")


def test_choose_naming_offers_renumbering_when_numbers_repeat(monkeypatch):
    cli = _menu_sequence(monkeypatch, [2])
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: True)
    clashing = [{"guid": "a", "episode_number": 1, "season_number": 1,
                 "published_at": "2024-01-01T00:00:00+00:00"},
                {"guid": "b", "episode_number": 1, "season_number": 2,
                 "published_at": "2024-02-01T00:00:00+00:00"}]
    assert cli._choose_naming(clashing, {}) == ("ep", "renumber")


def test_choose_naming_warns_about_s3_without_local_media(monkeypatch, capsys):
    cli = _menu_sequence(monkeypatch, [2])
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: True)
    cli._choose_naming(EPISODES, {"hosting": "s3", "keep_local_media": False})
    printed = " ".join(capsys.readouterr().out.split())  # undo terminal line wrapping
    assert "Keep a local copy of media" in printed
