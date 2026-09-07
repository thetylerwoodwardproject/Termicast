import json
from types import SimpleNamespace
from unittest.mock import Mock
import xml.etree.ElementTree as ET

import pytest
import requests

import cli
import mirror
import store
import transfer
from tests.test_transfer import Response


SOURCE_URL = 'https://source.test/feed.xml'
AUDIO_URL = 'https://source.test/episode.mp3'
BASE_URL = 'https://local.test'
SOURCE = f'''<rss version="2.0"
    xmlns:atom="http://www.w3.org/2005/Atom"
    xmlns:podcast="https://podcastindex.org/namespace/1.0">
<channel><title>Test show</title>
<atom:link rel="self" href="{SOURCE_URL}"/>
<item><title>Episode</title><guid>episode-guid</guid>
<pubDate>Wed, 01 Jan 2020 08:00:00 GMT</pubDate>
<enclosure url="{AUDIO_URL}" length="999" type="audio/mpeg"/>
</item></channel></rss>'''


@pytest.fixture(autouse=True)
def network(monkeypatch):
    get = Mock(side_effect=AssertionError('unexpected network request'))
    monkeypatch.setattr(requests, 'get', get)
    monkeypatch.setattr(transfer.time, 'sleep', Mock())
    return get


@pytest.fixture()
def import_prompts(monkeypatch):
    monkeypatch.setattr(cli, 'prompt', lambda *a, **kw: SOURCE_URL)
    answers = {
        'Import show-level metadata from this feed?': False,
        'Import all 1 episodes?': True,
        'Download audio files?': True,
        'Download episode artwork?': False,
        'Download transcripts (if available in feed)?': False,
    }
    monkeypatch.setattr(cli, 'prompt_bool', lambda question, *a, **kw: answers[question])


def feed_response(source=SOURCE):
    return SimpleNamespace(text=source, encoding='utf-8', raise_for_status=lambda: None)


@pytest.mark.parametrize('with_guid', [True, False], ids=['guid', 'audio-url-fallback'])
def test_import_rerun_deduplicates(data_dir, import_prompts, network, capsys, with_guid):
    store.save_show({'baseUrl': BASE_URL})
    source = SOURCE if with_guid else SOURCE.replace('<guid>episode-guid</guid>', '')
    audio = Response((b'audio',))
    network.side_effect = [feed_response(source), audio]

    cli.import_from_rss()

    episodes = store.get_episodes()
    assert len(episodes) == 1
    assert episodes[0]['guid'] == ('episode-guid' if with_guid else AUDIO_URL)
    assert episodes[0]['filesize'] == 5
    assert '1 imported, 0 skipped' in capsys.readouterr().out
    assert audio.closed

    # A stable GUID must deduplicate even if the enclosure URL changes.
    rerun_source = source.replace(AUDIO_URL, 'https://source.test/moved.mp3') if with_guid else source
    network.reset_mock()
    network.side_effect = [feed_response(rerun_source)]
    cli.import_from_rss()

    assert store.get_episodes() == episodes
    assert '0 imported, 1 skipped' in capsys.readouterr().out
    assert [call.args[0] for call in network.call_args_list] == [SOURCE_URL]
    assert (data_dir / 'media' / 'episode.mp3').read_bytes() == b'audio'
    assert not (data_dir / 'media' / 'moved.mp3').exists()
    assert len(ET.parse(data_dir / 'feed.xml').findall('channel/item')) == 1


def test_import_failed_audio_skips_then_retries_next_import(
        data_dir, import_prompts, network, capsys):
    store.save_show({'baseUrl': BASE_URL})
    broken = [Response((b'partial', requests.exceptions.ChunkedEncodingError('interrupted')))
              for _ in range(3)]
    network.side_effect = [feed_response(), *broken]

    cli.import_from_rss()

    assert store.get_episodes() == []
    assert list((data_dir / 'media').iterdir()) == []
    assert all(response.closed for response in broken)
    assert [call.args[0] for call in network.call_args_list] == [SOURCE_URL] + [AUDIO_URL] * 3
    output = capsys.readouterr().out
    assert 'Audio download failed, skipping episode; re-import to retry.' in output
    assert '0 imported, 1 skipped' in output
    assert ET.parse(data_dir / 'feed.xml').findall('channel/item') == []

    network.reset_mock()
    good = Response((b'complete audio',))
    network.side_effect = [feed_response(), good]
    cli.import_from_rss()

    episodes = store.get_episodes()
    assert len(episodes) == 1
    assert episodes[0]['guid'] == 'episode-guid'
    assert episodes[0]['filesize'] == len(b'complete audio')
    assert (data_dir / 'media' / episodes[0]['filename']).read_bytes() == b'complete audio'
    assert good.closed
    assert [call.args[0] for call in network.call_args_list] == [SOURCE_URL, AUDIO_URL]
    assert '1 imported, 0 skipped' in capsys.readouterr().out
    assert len(ET.parse(data_dir / 'feed.xml').findall('channel/item')) == 1


@pytest.fixture()
def mirror_config(data_dir):
    store.save_show({'baseUrl': BASE_URL + '/'})
    data = store.load()
    data['mirror'] = {'sourceUrl': SOURCE_URL}
    store.save(data)
    return data_dir / 'mirror' / 'media'


def test_mirror_retries_interrupted_download_rewrites_urls_and_skips_cache(
        mirror_config, network):
    broken = Response((b'partial', requests.exceptions.ChunkedEncodingError('interrupted')))
    good = Response((b'complete audio',), {'Content-Length': '14'})
    network.side_effect = [feed_response(), broken, good]
    logs = []

    stats = mirror.sync_mirror(log=logs.append)

    assert stats == {'assets_ok': 1, 'assets_failed': 0,
                     'feed_url': BASE_URL + '/mirror/feed.xml'}
    assert [call.args[0] for call in network.call_args_list] == [SOURCE_URL, AUDIO_URL, AUDIO_URL]
    assert all(call.kwargs['stream'] for call in network.call_args_list[1:])
    assert broken.closed and good.closed
    transfer.time.sleep.assert_called_once_with(0.5)
    name = mirror.local_asset_name(AUDIO_URL, 'audio/mpeg')
    dest = mirror_config / name
    assert dest.read_bytes() == b'complete audio'
    assert list(mirror_config.iterdir()) == [dest]
    root = ET.parse(mirror.MIRROR_FEED_FILE)
    assert root.find('channel/item/enclosure').get('url') == BASE_URL + '/mirror/media/' + name
    assert root.find('channel/{http://www.w3.org/2005/Atom}link').get('href') == stats['feed_url']
    assert root.findtext('channel/item/guid') == 'episode-guid'
    original_stat = dest.stat()

    network.reset_mock()
    network.side_effect = [feed_response()]
    assert mirror.sync_mirror(log=logs.append) == stats
    assert [call.args[0] for call in network.call_args_list] == [SOURCE_URL]
    assert dest.read_bytes() == b'complete audio'
    assert dest.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert ET.parse(mirror.MIRROR_FEED_FILE).find('channel/item/enclosure').get('url') == (
        BASE_URL + '/mirror/media/' + name)


@pytest.mark.parametrize('failure', ['invalid-json', 'chapter-image'])
def test_mirror_counts_chapter_failures(mirror_config, network, failure):
    chapters_url = 'https://source.test/chapters.json'
    image_url = 'https://source.test/chapter.png'
    source = SOURCE.replace(
        '</item>', f'<podcast:chapters url="{chapters_url}" type="application/json+chapters"/></item>')
    cached_audio = mirror_config / mirror.local_asset_name(AUDIO_URL, 'audio/mpeg')
    cached_audio.write_bytes(b'cached audio')
    payload = b'not json' if failure == 'invalid-json' else json.dumps({
        'chapters': [{'startTime': 0, 'img': image_url}]}).encode()
    chapters = Response((payload,))
    responses = [feed_response(source), chapters]
    if failure == 'chapter-image':
        responses.extend([requests.ConnectionError('image unavailable')] * 3)
    network.side_effect = responses
    logs = []

    stats = mirror.sync_mirror(log=logs.append)

    assert stats['assets_ok'] == 2
    assert stats['assets_failed'] == 1
    assert any('1 failures (including chapter assets)' in line for line in logs)
    assert chapters.closed
    assert [call.args[0] for call in network.call_args_list] == (
        [SOURCE_URL, chapters_url] + ([image_url] * 3 if failure == 'chapter-image' else []))
    name = mirror.local_asset_name(chapters_url, 'application/json+chapters')
    root = ET.parse(mirror.MIRROR_FEED_FILE)
    chapter_tag = root.find('channel/item/{https://podcastindex.org/namespace/1.0}chapters')
    assert chapter_tag.get('url') == BASE_URL + '/mirror/media/' + name
    assert (mirror_config / name).read_bytes() == payload
    assert set(mirror_config.iterdir()) == {cached_audio, mirror_config / name}
    if failure == 'chapter-image':
        assert json.loads((mirror_config / name).read_text())['chapters'][0]['img'] == image_url
