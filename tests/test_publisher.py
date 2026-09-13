from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from lxml import etree

from termicast.publisher import Publisher
from termicast.models import new_episode, new_show


@pytest.fixture(autouse=True)
def _no_s3_access_check(monkeypatch):
    """Neutralize the S3 access preflight; tests assert on deploy_paths instead."""
    from termicast import s3deploy
    monkeypatch.setattr(s3deploy, "check_s3_access", lambda show: None)


def make_episode(**kwargs):
    return new_episode(
        title="Ep", description="desc", mp3_url="https://example.org/show/audio/e1.mp3",
        length=1234, duration=60.0, slug="s01e001", audio_path="audio/e1.mp3",
        published_at="2024-01-01T00:00:00+00:00", **kwargs,
    )


def test_publish_local_writes_feed_and_chapters(db, show):
    episode = make_episode(
        chapters=[{"startTime": 0, "endTime": 60, "title": "Opening"}],
    )
    guid = Publisher(db).publish(show["id"], episode)
    assert guid == episode["guid"]
    feed = Path(show["output_dir"]) / "feed.xml"
    assert feed.is_file()
    root = etree.fromstring(feed.read_bytes())
    assert root.findtext("channel/item/guid") == guid
    assert (Path(show["output_dir"]) / "chapters" / "s01e001.json").is_file()
    saved = db.list_episodes(show["id"])
    assert saved[0]["status"] == "published"


@pytest.mark.parametrize("explicit_end", [False, True])
def test_published_chapters_preserve_only_supplied_ends(db, show, explicit_end):
    chapters = [{"startTime": 0, "title": "Opening"}, {"startTime": 30, "title": "Main"}]
    if explicit_end:
        chapters[0]["endTime"] = 20
    Publisher(db).publish(show["id"], make_episode(chapters=chapters))
    payload = json.loads((Path(show["output_dir"]) / "chapters/s01e001.json").read_text())
    assert payload["chapters"] == chapters
    assert db.list_episodes(show["id"])[0]["chapters"] == chapters
    root = etree.parse(str(Path(show["output_dir"]) / "feed.xml"))
    from termicast.feed import NS
    emitted = root.findall("channel/item/psc:chapters/psc:chapter", NS)
    assert len(emitted) == 2
    assert all(set(chapter.attrib) == {"start", "title"} for chapter in emitted)


def test_scheduled_episode_absent_from_feed(db, show):
    episode = make_episode()
    when = datetime.now(timezone.utc) + timedelta(days=7)
    Publisher(db).publish(show["id"], episode, when=when)
    feed = Path(show["output_dir"]) / "feed.xml"
    if feed.exists():
        root = etree.fromstring(feed.read_bytes())
        assert root.find("channel/item") is None
    saved = db.list_episodes(show["id"])
    assert saved[0]["status"] == "scheduled"


def test_publish_due_releases_scheduled(db, show):
    episode = make_episode()
    when = datetime.now(timezone.utc) + timedelta(days=1)
    Publisher(db).publish(show["id"], episode, when=when)
    assert db.list_episodes(show["id"])[0]["status"] == "scheduled"
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with db.connection() as conn:
        conn.execute("UPDATE episodes SET publish_at=? WHERE show_id=? AND guid=?",
                     (past, show["id"], episode["guid"]))
    count = Publisher(db).publish_due()
    assert count == 1
    assert db.list_episodes(show["id"])[0]["status"] == "published"


def test_deploy_local_returns_paths(db, show):
    Publisher(db).publish(show["id"], make_episode())
    paths = Publisher(db).deploy(show["id"], verify=False)
    assert "feed.xml" in paths
    assert "audio/e1.mp3" in paths


def _write_local_audio(show):
    path = Path(show["output_dir"]) / "audio" / "e1.mp3"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"audio-data")
    return path


def test_s3_publish_uploads_assets_not_feed(db, show, monkeypatch):
    show = dict(show, hosting="s3", bucket="b", prefix="p", asset_base_url="https://cdn.example.org/show", enabled=True)
    db.save_show(show)
    audio = _write_local_audio(show)
    uploaded = []
    from termicast import s3deploy
    monkeypatch.setattr(s3deploy, "deploy_paths",
                        lambda s, paths, dry_run=False, verify=True: uploaded.append(list(paths)) or list(paths))
    Publisher(db).publish(show["id"], make_episode())
    assert uploaded, "deploy_paths should have been called"
    assert "feed.xml" not in uploaded[0]
    assert "audio/e1.mp3" in uploaded[0]
    assert (Path(show["output_dir"]) / "feed.xml").is_file()
    # Default keep_local_media=False removes the uploaded working copy.
    assert not audio.exists()


def test_s3_publish_uploads_chapter_images(db, show, monkeypatch):
    """A chapter image staged under the show's own asset base must be uploaded too.

    Regression test: chapter "img" URLs used to be dropped from the deploy set
    entirely, so chapters.json referenced images that were never pushed to S3.
    """
    show = dict(show, hosting="s3", bucket="b", prefix="p", asset_base_url="https://cdn.example.org/show", enabled=True)
    db.save_show(show)
    image = Path(show["output_dir"]) / "images" / "chapters" / "c1.jpg"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"image-data")
    uploaded = []
    from termicast import s3deploy
    monkeypatch.setattr(s3deploy, "deploy_paths",
                        lambda s, paths, dry_run=False, verify=True: uploaded.append(list(paths)) or list(paths))
    episode = make_episode(chapters=[
        {"startTime": 0, "endTime": 60, "title": "Opening",
         "img": "https://cdn.example.org/show/images/chapters/c1.jpg"},
    ])
    Publisher(db).publish(show["id"], episode)
    assert uploaded, "deploy_paths should have been called"
    assert "images/chapters/c1.jpg" in uploaded[0]


def test_s3_publish_uploads_show_artwork(db, show, monkeypatch):
    """The show's own cover art staged locally must be uploaded too.

    Regression test: show-level artwork_url was dropped from the deploy set
    entirely (only episode artwork_url was collected), so an imported show's
    cover image was staged locally but never reached S3.
    """
    show = dict(show, hosting="s3", bucket="b", prefix="p", asset_base_url="https://cdn.example.org/show",
                enabled=True, artwork_url="https://cdn.example.org/show/images/cover.jpg")
    db.save_show(show)
    image = Path(show["output_dir"]) / "images" / "cover.jpg"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"image-data")
    uploaded = []
    from termicast import s3deploy
    monkeypatch.setattr(s3deploy, "deploy_paths",
                        lambda s, paths, dry_run=False, verify=True: uploaded.append(list(paths)) or list(paths))
    Publisher(db).publish(show["id"], make_episode())
    assert uploaded, "deploy_paths should have been called"
    assert "images/cover.jpg" in uploaded[0]


def test_deploy_standalone_uploads_show_artwork(db, show, monkeypatch):
    show = dict(show, hosting="s3", bucket="b", prefix="p", asset_base_url="https://cdn.example.org/show",
                enabled=True, keep_local_media=True, artwork_url="https://cdn.example.org/show/images/cover.jpg")
    db.save_show(show)
    image = Path(show["output_dir"]) / "images" / "cover.jpg"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"image-data")
    uploaded = []
    from termicast import s3deploy
    monkeypatch.setattr(s3deploy, "deploy_paths",
                        lambda s, paths, dry_run=False, verify=True: uploaded.append(list(paths)) or list(paths))
    Publisher(db).publish(show["id"], make_episode())
    uploaded.clear()
    Publisher(db).deploy(show["id"], verify=False)
    assert any("images/cover.jpg" in batch for batch in uploaded)


def test_s3_keep_local_media_retains_working_copy(db, show, monkeypatch):
    show = dict(show, hosting="s3", bucket="b", prefix="p", asset_base_url="https://cdn.example.org/show",
                enabled=True, keep_local_media=True)
    db.save_show(show)
    audio = _write_local_audio(show)
    from termicast import s3deploy
    monkeypatch.setattr(s3deploy, "deploy_paths", lambda *a, **k: list(a[1]))
    Publisher(db).publish(show["id"], make_episode())
    assert audio.exists()


def test_s3_disabled_skips_auto_deploy(db, show, monkeypatch):
    show = dict(show, hosting="s3", bucket="b", prefix="p", asset_base_url="https://cdn.example.org/show", enabled=False)
    db.save_show(show)
    calls = []
    from termicast import s3deploy
    monkeypatch.setattr(s3deploy, "deploy_paths", lambda *a, **k: calls.append(a))
    Publisher(db).publish(show["id"], make_episode())
    assert calls == []
    assert db.list_episodes(show["id"])[0]["status"] == "published"


def test_s3_publish_mirrors_feed_when_enabled(db, show, monkeypatch):
    show = dict(show, hosting="s3", bucket="b", prefix="p", asset_base_url="https://cdn.example.org/show",
                enabled=True, mirror_feed=True, keep_local_media=True)
    db.save_show(show)
    _write_local_audio(show)
    calls = []
    from termicast import s3deploy
    monkeypatch.setattr(s3deploy, "deploy_paths",
                        lambda s, paths, dry_run=False, verify=True: calls.append(list(paths)) or list(paths))
    Publisher(db).publish(show["id"], make_episode())
    assert calls == [["audio/e1.mp3"], ["feed.xml"]]
    assert (Path(show["output_dir"]) / "feed.xml").is_file()


def test_s3_publish_mirrors_fresh_feed_bytes(db, show, monkeypatch):
    show = dict(show, hosting="s3", bucket="b", prefix="p", asset_base_url="https://cdn.example.org/show",
                enabled=True, mirror_feed=True, keep_local_media=True)
    db.save_show(show)
    _write_local_audio(show)
    stale = Path(show["output_dir"]) / "feed.xml"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"<stale/>")
    from termicast import s3deploy
    monkeypatch.setattr(s3deploy, "deploy_paths", lambda s, paths, dry_run=False, verify=True: list(paths))
    episode = make_episode()
    Publisher(db).publish(show["id"], episode)
    content = stale.read_bytes()
    assert b"<stale/>" not in content
    assert episode["guid"].encode() in content


def test_s3_mirror_retains_feed_when_media_not_kept(db, show, monkeypatch):
    show = dict(show, hosting="s3", bucket="b", prefix="p", asset_base_url="https://cdn.example.org/show",
                enabled=True, mirror_feed=True)
    db.save_show(show)
    audio = _write_local_audio(show)
    from termicast import s3deploy
    monkeypatch.setattr(s3deploy, "deploy_paths", lambda s, paths, dry_run=False, verify=True: list(paths))
    Publisher(db).publish(show["id"], make_episode())
    assert (Path(show["output_dir"]) / "feed.xml").is_file()
    assert not audio.exists()


def test_s3_disabled_blocks_automatic_mirror(db, show, monkeypatch):
    show = dict(show, hosting="s3", bucket="b", prefix="p", asset_base_url="https://cdn.example.org/show",
                enabled=False, mirror_feed=True, keep_local_media=True)
    db.save_show(show)
    _write_local_audio(show)
    calls = []
    from termicast import s3deploy
    monkeypatch.setattr(s3deploy, "deploy_paths", lambda *a, **k: calls.append(a))
    Publisher(db).publish(show["id"], make_episode())
    assert calls == []


def test_local_hosting_never_mirrors(db, show, monkeypatch):
    show = dict(show, hosting="local", mirror_feed=True)
    db.save_show(show)
    calls = []
    from termicast import s3deploy
    monkeypatch.setattr(s3deploy, "deploy_paths", lambda *a, **k: calls.append(a))
    Publisher(db).publish(show["id"], make_episode())
    assert calls == []


def test_s3_media_failure_prevents_mirror(db, show, monkeypatch):
    show = dict(show, hosting="s3", bucket="b", prefix="p", asset_base_url="https://cdn.example.org/show",
                enabled=True, mirror_feed=True, keep_local_media=True)
    db.save_show(show)
    _write_local_audio(show)
    from termicast import s3deploy

    def fail(s, paths, dry_run=False, verify=True):
        if paths == ["feed.xml"]:
            raise AssertionError("mirror must not run after media failure")
        raise RuntimeError("media boom")
    monkeypatch.setattr(s3deploy, "deploy_paths", fail)
    with pytest.raises(RuntimeError, match="publish-due"):
        Publisher(db).publish(show["id"], make_episode())
    assert db.list_episodes(show["id"])[0]["status"] == "scheduled"


def test_s3_mirror_failure_keeps_feed_and_dirty_state(db, show, monkeypatch):
    show = dict(show, hosting="s3", bucket="b", prefix="p", asset_base_url="https://cdn.example.org/show",
                enabled=True, mirror_feed=True, keep_local_media=True)
    db.save_show(show)
    _write_local_audio(show)
    episode = make_episode()
    from termicast import s3deploy

    def fail_mirror(s, paths, dry_run=False, verify=True):
        if paths == ["feed.xml"]:
            raise RuntimeError("mirror boom")
        return list(paths)
    monkeypatch.setattr(s3deploy, "deploy_paths", fail_mirror)
    with pytest.raises(RuntimeError, match="publish-due"):
        Publisher(db).publish(show["id"], episode)
    feed = Path(show["output_dir"]) / "feed.xml"
    assert feed.is_file()
    assert episode["guid"].encode() in feed.read_bytes()
    with db.connection() as conn:
        dirty = conn.execute("SELECT dirty FROM shows WHERE id=?", (show["id"],)).fetchone()[0]
    assert dirty == 1

    monkeypatch.setattr(s3deploy, "deploy_paths", lambda s, paths, dry_run=False, verify=True: list(paths))
    Publisher(db).publish_due()
    assert db.list_episodes(show["id"])[0]["status"] == "published"
    with db.connection() as conn:
        dirty = conn.execute("SELECT dirty FROM shows WHERE id=?", (show["id"],)).fetchone()[0]
    assert dirty == 0


def test_s3_deploy_mirrors_feed_after_media(db, show, monkeypatch):
    show = dict(show, hosting="s3", bucket="b", prefix="p", asset_base_url="https://cdn.example.org/show",
                enabled=False, mirror_feed=True, keep_local_media=True)
    db.save_show(show)
    Publisher(db).publish(show["id"], make_episode())
    _write_local_audio(show)
    calls = []
    from termicast import s3deploy
    monkeypatch.setattr(s3deploy, "deploy_paths",
                        lambda s, paths, dry_run=False, verify=True: calls.append(list(paths)) or list(paths))
    result = Publisher(db).deploy(show["id"], verify=False)
    assert calls == [["audio/e1.mp3"], ["feed.xml"]]
    assert result == ["audio/e1.mp3", "feed.xml"]


def test_s3_deploy_missing_feed_fails_before_media(db, show, monkeypatch):
    show = dict(show, hosting="s3", bucket="b", prefix="p", asset_base_url="https://cdn.example.org/show",
                mirror_feed=True)
    db.save_show(show)
    calls = []
    from termicast import s3deploy
    monkeypatch.setattr(s3deploy, "deploy_paths", lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError, match="No local feed.xml"):
        Publisher(db).deploy(show["id"], verify=False)
    assert calls == []


def test_s3_deploy_dry_run_has_no_side_effects(db, show, monkeypatch):
    show = dict(show, hosting="s3", bucket="b", prefix="p", asset_base_url="https://cdn.example.org/show",
                enabled=False, mirror_feed=True, keep_local_media=True)
    db.save_show(show)
    Publisher(db).publish(show["id"], make_episode())
    _write_local_audio(show)
    calls = []
    from termicast import s3deploy
    monkeypatch.setattr(s3deploy, "deploy_paths",
                        lambda s, paths, dry_run=False, verify=True: calls.append((list(paths), dry_run, verify)) or list(paths))
    Publisher(db).deploy(show["id"], dry_run=True)
    assert all(dry_run for _, dry_run, _ in calls)
    assert (Path(show["output_dir"]) / "audio" / "e1.mp3").exists()
    assert (Path(show["output_dir"]) / "feed.xml").is_file()


def test_s3_deploy_no_verify_suppresses_checks(db, show, monkeypatch):
    show = dict(show, hosting="s3", bucket="b", prefix="p", asset_base_url="https://cdn.example.org/show",
                enabled=False, mirror_feed=True, keep_local_media=True)
    db.save_show(show)
    Publisher(db).publish(show["id"], make_episode())
    _write_local_audio(show)
    verifies = []
    from termicast import s3deploy
    monkeypatch.setattr(s3deploy, "deploy_paths",
                        lambda s, paths, dry_run=False, verify=True: verifies.append(verify) or list(paths))
    Publisher(db).deploy(show["id"], verify=False)
    assert verifies == [False, False]


def test_s3_deploy_verify_reports_unreachable_published_asset(db, show, monkeypatch):
    show = dict(show, hosting="s3", bucket="b", prefix="p", asset_base_url="https://cdn.example.org/show",
                enabled=False, keep_local_media=True)
    db.save_show(show)
    Publisher(db).publish(show["id"], make_episode())
    _write_local_audio(show)
    from termicast import hosting, s3deploy
    monkeypatch.setattr(s3deploy, "deploy_paths", lambda *a, **k: list(a[1]))
    monkeypatch.setattr(hosting, "check_assets",
                        lambda s, e, skip=(): ["Unreachable: https://cdn.example.org/show/audio/e1.mp3"])
    with pytest.raises(RuntimeError, match="Unreachable"):
        Publisher(db).deploy(show["id"])


def test_s3_deploy_verify_passes_when_assets_reachable(db, show, monkeypatch):
    show = dict(show, hosting="s3", bucket="b", prefix="p", asset_base_url="https://cdn.example.org/show",
                enabled=False, keep_local_media=True)
    db.save_show(show)
    Publisher(db).publish(show["id"], make_episode())
    _write_local_audio(show)
    from termicast import hosting, s3deploy
    monkeypatch.setattr(s3deploy, "deploy_paths", lambda *a, **k: list(a[1]))
    verified = []
    monkeypatch.setattr(hosting, "check_assets",
                        lambda s, e, skip=(): verified.append((e, skip)) or [])
    result = Publisher(db).deploy(show["id"])
    assert result == ["audio/e1.mp3"]
    assert len(verified) == 1
    assert verified[0][0][0]["mp3_url"]
    # The upload already verified this URL, so the sweep must not recheck it.
    assert verified[0][1] == {"https://cdn.example.org/show/audio/e1.mp3"}


def test_s3_deploy_preflights_access_before_upload(db, show, monkeypatch):
    show = dict(show, hosting="s3", bucket="b", prefix="p", asset_base_url="https://cdn.example.org/show",
                enabled=False, keep_local_media=True)
    db.save_show(show)
    Publisher(db).publish(show["id"], make_episode())
    _write_local_audio(show)
    from termicast import s3deploy
    checks = []
    monkeypatch.setattr(s3deploy, "check_s3_access", lambda s: checks.append(s) or None)
    monkeypatch.setattr(s3deploy, "deploy_paths", lambda s, paths, dry_run=False, verify=True: list(paths))
    Publisher(db).deploy(show["id"], verify=False)
    assert len(checks) == 1
    assert checks[0]["hosting"] == "s3"


def test_s3_deploy_access_failure_aborts_before_upload(db, show, monkeypatch):
    show = dict(show, hosting="s3", bucket="b", prefix="p", asset_base_url="https://cdn.example.org/show",
                enabled=False, keep_local_media=True)
    db.save_show(show)
    Publisher(db).publish(show["id"], make_episode())
    _write_local_audio(show)
    from termicast import s3deploy
    monkeypatch.setattr(s3deploy, "check_s3_access",
                        lambda s: (_ for _ in ()).throw(RuntimeError("no credentials")))
    uploads = []
    monkeypatch.setattr(s3deploy, "deploy_paths",
                        lambda s, paths, dry_run=False, verify=True: uploads.append(paths) or list(paths))
    with pytest.raises(RuntimeError, match="no credentials"):
        Publisher(db).deploy(show["id"], verify=False)
    assert uploads == []


def test_s3_deploy_dry_run_skips_access_preflight(db, show, monkeypatch):
    show = dict(show, hosting="s3", bucket="b", prefix="p", asset_base_url="https://cdn.example.org/show",
                enabled=False, mirror_feed=True, keep_local_media=True)
    db.save_show(show)
    Publisher(db).publish(show["id"], make_episode())
    _write_local_audio(show)
    from termicast import s3deploy
    checks = []
    monkeypatch.setattr(s3deploy, "check_s3_access", lambda s: checks.append(s) or None)
    monkeypatch.setattr(s3deploy, "deploy_paths", lambda s, paths, dry_run=False, verify=True: list(paths))
    Publisher(db).deploy(show["id"], dry_run=True)
    assert checks == []


def test_s3_publish_preflights_access_before_upload(db, show, monkeypatch):
    show = dict(show, hosting="s3", bucket="b", prefix="p", asset_base_url="https://cdn.example.org/show",
                enabled=True, keep_local_media=True)
    db.save_show(show)
    _write_local_audio(show)
    from termicast import s3deploy
    checks = []
    monkeypatch.setattr(s3deploy, "check_s3_access", lambda s: checks.append(s) or None)
    monkeypatch.setattr(s3deploy, "deploy_paths", lambda s, paths, dry_run=False, verify=True: list(paths))
    Publisher(db).publish(show["id"], make_episode())
    assert len(checks) == 1


def test_locks_live_under_asset_root_for_a_tilde_output_dir(tmp_path, monkeypatch):
    """Lock files must sit beside the assets they guard, not in a literal `~`.

    save_show() normalizes output_dir, so this is not reachable from the CLI
    today, but a show dict that reaches a lock without being saved first used
    to build `./~/podcast/.termicast.oplock` relative to the CWD -- a
    different path than asset_root(), which defeats the mutual exclusion.
    """
    from termicast.publisher import operation_lock, output_lock
    from termicast.storage import asset_root

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    show = new_show(title="S", description="d", base_url="https://e.org/s",
                    output_dir="~/podcast")
    root = asset_root(show)
    root.mkdir(parents=True)

    with operation_lock(show), output_lock(show):
        pass

    assert (root / ".termicast.oplock").is_file()
    assert (root / ".termicast.lock").is_file()
    assert not (Path.cwd() / "~").exists()
