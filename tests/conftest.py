import pytest

import cli
import feed
import mirror
import promote
import store


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """Keep test data and generated output away from the real data directory."""
    d = tmp_path / 'termicast_data'
    (d / 'media').mkdir(parents=True)
    (d / 'mirror' / 'media').mkdir(parents=True)
    monkeypatch.setenv('TERMICAST_DATA_DIR', str(d))
    for module in (store, feed, mirror, promote):
        monkeypatch.setattr(module, 'BASE_DIR', str(d))
    monkeypatch.setattr(store, 'DATA_FILE', str(d / 'podcast.json'))
    monkeypatch.setattr(feed, 'FEED_FILE', str(d / 'feed.xml'))
    for module in (feed, promote, cli):
        monkeypatch.setattr(module, 'MEDIA_DIR', str(d / 'media'))
    monkeypatch.setattr(mirror, 'MIRROR_DIR', str(d / 'mirror'))
    monkeypatch.setattr(mirror, 'MIRROR_MEDIA_DIR', str(d / 'mirror' / 'media'))
    monkeypatch.setattr(mirror, 'MIRROR_FEED_FILE', str(d / 'mirror' / 'feed.xml'))
    return d
