from datetime import datetime, timedelta, timezone
from pathlib import Path

from lxml import etree

from termicast.publisher import Publisher
from termicast.models import new_episode
from termicast.feed import _tag, NS


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


def test_s3_publish_uploads_feed_last(db, show, monkeypatch):
    show = dict(show, hosting="s3", bucket="b", prefix="p", enabled=True)
    db.save_show(show)
    uploaded = []
    from termicast import s3deploy
    monkeypatch.setattr(s3deploy, "deploy_paths",
                        lambda s, paths, dry_run=False, verify=True: uploaded.append(list(paths)))
    Publisher(db).publish(show["id"], make_episode())
    assert uploaded, "deploy_paths should have been called"
    assert uploaded[0][-1] == "feed.xml"


def test_s3_disabled_skips_auto_deploy(db, show, monkeypatch):
    show = dict(show, hosting="s3", bucket="b", prefix="p", enabled=False)
    db.save_show(show)
    calls = []
    from termicast import s3deploy
    monkeypatch.setattr(s3deploy, "deploy_paths", lambda *a, **k: calls.append(a))
    Publisher(db).publish(show["id"], make_episode())
    assert calls == []
    assert db.list_episodes(show["id"])[0]["status"] == "published"
