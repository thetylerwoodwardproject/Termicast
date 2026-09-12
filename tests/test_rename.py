"""Renaming one episode's managed assets."""

import json
import os
from pathlib import Path

import pytest

from termicast.models import new_episode
from termicast.publisher import Publisher
from termicast.rename import clear_orphans, plan_rename, rename_episode
from termicast.storage import asset_root, local_relative


BASE = "https://example.org/show"


@pytest.fixture
def episode(db, show):
    """A published episode with audio, artwork, and a transcript on disk."""
    assets = asset_root(show)
    for folder in ("audio", "images", "transcripts"):
        (assets / folder).mkdir(parents=True, exist_ok=True)
    (assets / "audio" / "old.mp3").write_bytes(b"audio")
    (assets / "images" / "old.jpg").write_bytes(b"image")
    (assets / "transcripts" / "old.vtt").write_text("WEBVTT\n")
    record = new_episode(
        guid="ep-1", title="Episode one", description="First", slug="old",
        mp3_url=f"{BASE}/audio/old.mp3", length=5, duration=60.0,
        artwork_url=f"{BASE}/images/old.jpg", transcript_url=f"{BASE}/transcripts/old.vtt",
        audio_path="audio/old.mp3", image_path="images/old.jpg",
        transcript_path="transcripts/old.vtt",
    )
    Publisher(db).publish(show["id"], record)
    return db.list_episodes(show["id"])[0]


def test_local_relative_reverses_asset_base(show):
    assert str(local_relative(show, f"{BASE}/audio/old.mp3")) == "audio/old.mp3"
    assert local_relative(show, "https://elsewhere.example/audio/old.mp3") is None
    assert local_relative(show, "") is None


def test_plan_moves_every_managed_asset(show, episode):
    plan = plan_rename(show, episode, "ep001")
    assert sorted(plan.moves) == [
        ("audio/old.mp3", "audio/ep001.mp3"),
        ("images/old.jpg", "images/ep001.jpg"),
        ("transcripts/old.vtt", "transcripts/ep001.vtt"),
    ]
    assert plan.fields["slug"] == "ep001"
    assert plan.fields["mp3_url"] == f"{BASE}/audio/ep001.mp3"
    assert plan.fields["audio_path"] == "audio/ep001.mp3"
    assert not plan.missing and not plan.blocked


def test_plan_derives_paths_from_urls_when_paths_are_absent(db, show):
    """Imported episodes carry URLs but no *_path fields."""
    assets = asset_root(show)
    (assets / "audio").mkdir(parents=True, exist_ok=True)
    (assets / "images" / "episodes").mkdir(parents=True, exist_ok=True)
    (assets / "audio" / "abc123.mp3").write_bytes(b"audio")
    (assets / "images" / "episodes" / "abc123.jpg").write_bytes(b"image")
    imported = new_episode(
        guid="ep-2", title="Imported", description="d", length=5, duration=60.0,
        mp3_url=f"{BASE}/audio/abc123.mp3",
        artwork_url=f"{BASE}/images/episodes/abc123.jpg",
    )
    plan = plan_rename(show, imported, "ep001")
    # Artwork stays in images/episodes: renaming never moves between folders.
    assert sorted(plan.moves) == [
        ("audio/abc123.mp3", "audio/ep001.mp3"),
        ("images/episodes/abc123.jpg", "images/episodes/ep001.jpg"),
    ]


def test_plan_leaves_assets_shared_with_another_episode(show, episode):
    other = dict(episode, guid="ep-9", image_path="images/old.jpg")
    plan = plan_rename(show, episode, "ep001", others=[other])
    assert ("images/old.jpg", "images/ep001.jpg") not in plan.moves
    assert any("shared" in message for message in plan.warnings)


def test_plan_reports_a_missing_file(show, episode):
    (asset_root(show) / "audio" / "old.mp3").unlink()
    plan = plan_rename(show, episode, "ep001")
    assert plan.missing == ["audio/old.mp3"]


def test_plan_routes_a_missing_s3_only_file_to_remote_moves(db, show, episode):
    """No local copy on S3 hosting is renamed in place, not reported missing."""
    show = db.save_show(dict(show, hosting="s3", bucket="b", asset_base_url=BASE))
    (asset_root(show) / "audio" / "old.mp3").unlink()
    plan = plan_rename(show, episode, "ep001")
    assert not plan.missing
    assert ("audio/old.mp3", "audio/ep001.mp3") in plan.remote_moves
    assert plan.fields["mp3_url"] == f"{BASE}/audio/ep001.mp3"


def test_plan_reports_a_blocked_destination(show, episode):
    (asset_root(show) / "audio" / "ep001.mp3").write_bytes(b"in the way")
    plan = plan_rename(show, episode, "ep001")
    assert plan.blocked == ["audio/ep001.mp3"]


def test_plan_rejects_an_unsafe_slug(show, episode):
    with pytest.raises(ValueError, match="Invalid slug"):
        plan_rename(show, episode, "../escape")


def test_rename_moves_files_and_rewrites_the_record(db, show, episode):
    plan = plan_rename(show, episode, "ep001")
    _, renamed = rename_episode(db, show, episode, plan)
    assets = asset_root(show)
    assert (assets / "audio" / "ep001.mp3").is_file()
    assert not (assets / "audio" / "old.mp3").exists()
    assert renamed["slug"] == "ep001"
    assert renamed["mp3_url"] == f"{BASE}/audio/ep001.mp3"
    stored = db.list_episodes(show["id"])[0]
    assert stored["audio_path"] == "audio/ep001.mp3"
    assert stored["status"] == "published"


def test_rename_refuses_when_a_file_is_missing(db, show, episode):
    (asset_root(show) / "audio" / "old.mp3").unlink()
    plan = plan_rename(show, episode, "ep001")
    with pytest.raises(ValueError, match="No local file to move"):
        rename_episode(db, show, episode, plan)
    assert db.list_episodes(show["id"])[0]["slug"] == "old"


def test_rename_rolls_back_a_partial_move(db, show, episode, monkeypatch):
    plan = plan_rename(show, episode, "ep001")
    calls = []
    real = os.replace

    def failing(src, dst):
        calls.append(src)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", failing)
    with pytest.raises(OSError):
        rename_episode(db, show, episode, plan)
    monkeypatch.undo()
    assets = asset_root(show)
    assert (assets / "audio" / "old.mp3").is_file()
    assert (assets / "images" / "old.jpg").is_file()
    assert not (assets / "audio" / "ep001.mp3").exists()
    assert db.list_episodes(show["id"])[0] == episode


def test_rename_calls_remote_rename_for_s3_only_assets(db, show, episode, monkeypatch):
    show = db.save_show(dict(show, hosting="s3", bucket="b", asset_base_url=BASE))
    (asset_root(show) / "audio" / "old.mp3").unlink()
    plan = plan_rename(show, episode, "ep001")
    calls = []
    monkeypatch.setattr("termicast.s3deploy.remote_rename",
                        lambda show, old, new: calls.append((old, new)))
    _, renamed = rename_episode(db, show, episode, plan)
    assert calls == [("audio/old.mp3", "audio/ep001.mp3")]
    assert renamed["mp3_url"] == f"{BASE}/audio/ep001.mp3"
    # The artwork and transcript did have local copies, so they still moved.
    assert (asset_root(show) / "images" / "ep001.jpg").is_file()


def test_rename_rolls_back_local_moves_when_a_remote_rename_fails(db, show, episode, monkeypatch):
    show = db.save_show(dict(show, hosting="s3", bucket="b", asset_base_url=BASE))
    (asset_root(show) / "audio" / "old.mp3").unlink()
    plan = plan_rename(show, episode, "ep001")

    def failing(show, old, new):
        raise RuntimeError("s4cmd unreachable")

    monkeypatch.setattr("termicast.s3deploy.remote_rename", failing)
    with pytest.raises(RuntimeError, match="s4cmd unreachable"):
        rename_episode(db, show, episode, plan)
    assets = asset_root(show)
    assert (assets / "images" / "old.jpg").is_file()
    assert not (assets / "images" / "ep001.jpg").exists()
    assert db.list_episodes(show["id"])[0]["slug"] == "old"


def test_rename_updates_the_import_url_map_in_place(db, show, episode):
    original = "https://old.example.org/audio/1.mp3"
    show = db.save_show(dict(show, import_url_map={original: f"{BASE}/audio/old.mp3"}))
    plan = plan_rename(show, episode, "ep001")
    updated, _ = rename_episode(db, show, episode, plan)
    assert updated["import_url_map"] == {original: f"{BASE}/audio/ep001.mp3"}


def test_rename_refuses_a_slug_another_episode_owns(db, show, episode):
    second = new_episode(guid="ep-2", title="Two", description="d", slug="ep001",
                         mp3_url=f"{BASE}/audio/two.mp3", length=5, duration=60.0)
    Publisher(db).publish(show["id"], second)
    plan = plan_rename(show, episode, "ep001")
    with pytest.raises(ValueError, match="already used"):
        rename_episode(db, show, episode, plan)


def test_rename_inserts_a_row_for_a_template_only_episode(db, show):
    """list_episodes synthesizes these from the stored template; renaming needs a row."""
    assets = asset_root(show)
    (assets / "audio").mkdir(parents=True, exist_ok=True)
    (assets / "audio" / "abc.mp3").write_bytes(b"audio")
    template = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
        '<channel><title>T</title><description>d</description>'
        '<link>https://example.org/show</link>'
        '<item><title>Only in the template</title><guid isPermaLink="false">tmpl-1</guid>'
        '<description>d</description>'
        f'<enclosure url="{BASE}/audio/abc.mp3" length="5" type="audio/mpeg"/>'
        '<pubDate>Sun, 01 Sep 2024 12:00:00 GMT</pubDate>'
        '<itunes:duration>60</itunes:duration></item></channel></rss>'
    ).encode()
    show = db.save_show(show, template=template)

    def rows():
        with db.connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM episodes WHERE show_id=?",
                                (show["id"],)).fetchone()[0]

    assert rows() == 0
    episode = db.list_episodes(show["id"])[0]
    assert episode["guid"] == "tmpl-1" and not episode["audio_path"]
    plan = plan_rename(show, episode, "ep001")
    rename_episode(db, show, episode, plan)
    assert rows() == 1
    assert db.list_episodes(show["id"])[0]["slug"] == "ep001"


def test_chapters_follow_the_slug_and_the_old_file_is_an_orphan(db, show):
    assets = asset_root(show)
    (assets / "audio").mkdir(parents=True, exist_ok=True)
    (assets / "audio" / "old.mp3").write_bytes(b"audio")
    record = new_episode(
        guid="ep-3", title="With chapters", description="d", slug="old",
        mp3_url=f"{BASE}/audio/old.mp3", length=5, duration=60.0,
        audio_path="audio/old.mp3",
        chapters=[{"startTime": 0.0, "endTime": 60.0, "title": "Start"}],
    )
    publisher = Publisher(db)
    publisher.publish(show["id"], record)
    stored = db.list_episodes(show["id"])[0]
    assert (assets / "chapters" / "old.json").is_file()

    plan = plan_rename(show, stored, "ep001")
    assert plan.orphans == ["chapters/old.json"]
    show_after, _ = rename_episode(db, show, stored, plan)
    publisher.regenerate(show["id"])
    assert (assets / "chapters" / "ep001.json").is_file()
    assert clear_orphans(show_after, plan) == ["chapters/old.json"]
    assert not (assets / "chapters" / "old.json").exists()


def test_renamed_imported_audio_becomes_deployable(db, show, episode):
    """Setting audio_path is what makes imported media visible to deploy."""
    from termicast.publisher import episode_asset_paths
    plan = plan_rename(show, episode, "ep001")
    _, renamed = rename_episode(db, show, episode, plan)
    assert "audio/ep001.mp3" in episode_asset_paths(renamed)


# --- editor wiring --------------------------------------------------------

def test_rename_form_cancel_changes_nothing(db, show, episode, monkeypatch):
    from termicast import prompts
    from termicast.prompts import Cancelled

    def raiser(*args, **kwargs):
        raise Cancelled()

    monkeypatch.setattr(prompts, "text", raiser)
    with pytest.raises(Cancelled):
        prompts.rename_form(db, show, Publisher(db), episode)
    assert (asset_root(show) / "audio" / "old.mp3").is_file()
    assert db.list_episodes(show["id"])[0]["slug"] == "old"


def test_rename_form_declining_a_published_rename_changes_nothing(db, show, episode, monkeypatch, capsys):
    from termicast import prompts
    monkeypatch.setattr(prompts, "text", lambda *a, **k: "ep001")
    monkeypatch.setattr(prompts, "confirm", lambda *a, **k: False)
    assert prompts.rename_form(db, show, Publisher(db), episode) is False
    output = capsys.readouterr().out
    # The warning must name both the old and the new public URL.
    assert "audio/old.mp3" in output and "audio/ep001.mp3" in output
    assert "already published" in output
    assert (asset_root(show) / "audio" / "old.mp3").is_file()


def test_rename_form_renames_on_confirmation(db, show, episode, monkeypatch):
    from termicast import prompts
    monkeypatch.setattr(prompts, "text", lambda *a, **k: "ep001")
    monkeypatch.setattr(prompts, "confirm", lambda *a, **k: True)
    assert prompts.rename_form(db, show, Publisher(db), episode) is True
    assert (asset_root(show) / "audio" / "ep001.mp3").is_file()
    assert db.list_episodes(show["id"])[0]["slug"] == "ep001"


def test_editor_requires_a_clean_buffer_before_renaming(db, show, episode, monkeypatch, capsys):
    """Renaming persists immediately, so unsaved edits must be resolved first."""
    from termicast import prompts
    steps = iter(["Edit field", "Rename files", "Cancel"])

    def fake_menu(title, options, *args, **kwargs):
        return options.index(next(steps)) + 1

    def fail(*args, **kwargs):
        pytest.fail("rename_form must not run with unsaved changes")

    monkeypatch.setattr(prompts, "menu", fake_menu)
    monkeypatch.setattr(prompts, "rename_form", fail)
    monkeypatch.setattr(prompts, "edit_menu",
                        lambda data, *a, **k: data.update(title="Edited in the buffer"))
    monkeypatch.setattr(prompts, "review", lambda *a, **k: None)
    prompts.edit_episode_form(db, show, Publisher(db), episode)
    assert "Save or cancel your other changes" in capsys.readouterr().out


def test_editor_option_labels_do_not_shift(db, show, episode, monkeypatch):
    """Published and unpublished editors expose different option counts."""
    from termicast import prompts
    captured = []
    monkeypatch.setattr(prompts, "review", lambda *a, **k: None)

    def fake_menu(title, options, *args, **kwargs):
        captured.append(list(options))
        return options.index("Cancel") + 1

    monkeypatch.setattr(prompts, "menu", fake_menu)
    prompts.edit_episode_form(db, show, Publisher(db), episode)
    assert captured[0] == ["Edit field", "Save", "Cancel", "Rename files", "Add optional assets"]

    captured.clear()
    scheduled = dict(episode, status="scheduled")
    prompts.edit_episode_form(db, show, Publisher(db), scheduled)
    assert captured[0] == ["Edit field", "Save", "Cancel", "Reschedule", "Publish now",
                           "Rename files", "Add optional assets"]
