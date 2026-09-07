from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

import cli
import feed
import mirror
import promote
import store


def episode(name, date):
    return {'id': name, 'guid': name, 'title': name, 'filename': name + '.mp3',
            'pubDate': date}


def titles():
    return [e['title'] for e in store.get_episodes()]


def test_store_and_feed_sort_instants(data_dir):
    store.add_episode(episode('earlier', '2020-01-01T10:00:00+02:00'))
    store.add_episode(episode('later', '2020-01-01T09:00:00Z'))
    store.add_episode(episode('legacy', '2020-01-01T08:30:00'))
    assert titles() == ['later', 'legacy', 'earlier']
    store.update_episode('earlier', {'pubDate': '2020-01-01T12:00:00+02:00'})
    assert titles() == ['earlier', 'later', 'legacy']
    data = store.load()
    data['episodes'].reverse()
    store.save(data)
    root = ET.fromstring(feed.generate_feed())
    assert [i.findtext('title') for i in root.findall('channel/item')] == titles()
    assert titles() == ['earlier', 'later', 'legacy']


@pytest.mark.parametrize('raw', [
    'Wed, 01 Jan 2020 08:00:00 GMT',
    '01 Jan 2020 10:00:00 +0200',
    'Wed, 01 Jan 2020 08:00:00 -0000',
])
def test_rss_dates(raw):
    assert store.parse_rss_date(raw) == '2020-01-01T08:00:00+00:00'


SOURCE = '''<rss version="2.0"><channel><title>Test</title>
<item><title>earlier</title><guid>earlier</guid>
<pubDate>01 Jan 2020 10:00:00 +0200</pubDate>
<enclosure url="https://source.test/earlier.mp3" length="1"/></item>
<item><title>later</title><guid>later</guid>
<pubDate>Wed, 01 Jan 2020 09:00:00 GMT</pubDate>
<enclosure url="https://source.test/later.mp3" length="1"/></item>
</channel></rss>'''


def test_mirror_ordering_preserves_raw_xml():
    prefix = '''<?xml version="1.0" encoding="UTF-8"?>
<rss version='2.0' xmlns:x='urn:unknown'><channel>
<title>Caf\u00e9</title><x:item><pubDate>invalid</pubDate></x:item>
<description><![CDATA[<item>not an episode</item>]]></description>
'''
    older = '''<item x:note='a > b' xmlns:y="urn:future">
<title>caf\u00e9 &amp; tea</title><pubDate>01 Jan 2020 10:00:00 +0200</pubDate>
<y:unknown a = 'b'><![CDATA[<item></item> & untouched]]></y:unknown>
<!-- </item> --><x:item><pubDate>01 Jan 2099 00:00:00 GMT</pubDate></x:item>
</item>'''
    newer = '''<item><title>newer</title>
<pubDate><![CDATA[01 Jan 2020 09:00:00 GMT]]></pubDate><x:unknown /></item>'''
    between = '\n<!-- stationary channel comment --><?keep this?><x:metadata/>\n'
    suffix = '\n</channel></rss>'
    source = prefix + older + between + newer + suffix
    expected = prefix + newer + between + older + suffix
    assert mirror.reorder_episode_items(source) == expected
    assert mirror.reorder_episode_items(expected) == expected


def test_mirror_ordering_dates_and_stability():
    dates = [
        ('invalid', 'nonsense'),
        ('equal-offset', '01 Jan 2020 10:00:00 +0200'),
        ('missing', None),
        ('newest', '01 Jan 2020 05:00:00 -0500'),
        ('equal-gmt', '01 Jan 2020 08:00:00 GMT'),
        ('impossible', '31 Feb 2020 00:00:00 GMT'),
        ('equal-naive', '01 Jan 2020 08:00:00 -0000'),
        ('oldest', '31 Dec 2019 23:00:00 GMT'),
        ('empty', ''),
        ('overflow', '31 Dec 9999 23:59:59 -0100'),
    ]
    blocks = {
        name: '<item><title>' + name + '</title>'
        + ('' if date is None else '<pubDate>' + date + '</pubDate>') + '</item>'
        for name, date in dates
    }
    order = ['newest', 'equal-offset', 'equal-gmt', 'equal-naive', 'oldest',
             'invalid', 'missing', 'impossible', 'empty', 'overflow']
    prefix, suffix = '<rss><channel>', '</channel></rss>'
    source = prefix + ''.join(blocks.values()) + suffix
    assert mirror.reorder_episode_items(source) == (
        prefix + ''.join(blocks[name] for name in order) + suffix)


@pytest.mark.parametrize('body', [
    '', '<item/>', '<item note=" > "/>', '<item></item>',
    '<item/><item></item><item />',
    '<x:wrapper xmlns:x="urn:x"><item/></x:wrapper>',
])
def test_mirror_ordering_unchanged_without_valid_dates(body):
    source = '<rss><channel>' + body + '</channel></rss>'
    assert mirror.reorder_episode_items(source) == source


def test_mirror_ordering_moves_empty_items_last():
    dated = '<item><pubDate>01 Jan 2020 08:00:00 GMT</pubDate></item>'
    source = '<rss><channel><item note=" > "/>' + dated + '<item/></channel></rss>'
    assert mirror.reorder_episode_items(source) == (
        '<rss><channel>' + dated + '<item note=" > "/><item/></channel></rss>')


def test_mirror_ordering_rejects_malformed_xml():
    with pytest.raises(mirror.expat.ExpatError):
        mirror.reorder_episode_items('<rss><channel><item></channel></rss>')


def test_cli_import_newest_first_and_skips_bad_dates(data_dir, monkeypatch, capsys):
    bad = '''<item><title>bad</title><pubDate>invalid</pubDate>
    <enclosure url="https://source.test/bad.mp3"/></item>'''
    source = SOURCE.replace('</channel>', bad + '</channel>')
    monkeypatch.setattr(cli.requests, 'get', lambda *a, **kw: SimpleNamespace(
        text=source, raise_for_status=lambda: None))
    monkeypatch.setattr(cli, 'prompt', lambda *a, **kw: 'https://source.test/feed.xml')
    answers = iter([False, True, False, False, False])
    monkeypatch.setattr(cli, 'prompt_bool', lambda *a, **kw: next(answers))
    cli.import_from_rss()
    assert titles() == ['later', 'earlier']
    assert '2 imported, 1 skipped' in capsys.readouterr().out
    assert all(e['pubDate'].endswith('+00:00') for e in store.get_episodes())


def test_mirror_newest_first_and_promotion_sorts(data_dir, monkeypatch):
    store.save_show({'baseUrl': 'https://local.test'})
    data = store.load()
    data['mirror'] = {'sourceUrl': 'https://source.test/feed.xml'}
    store.save(data)
    monkeypatch.setattr(mirror.requests, 'get', lambda *a, **kw: SimpleNamespace(
        text=SOURCE, encoding='utf-8', raise_for_status=lambda: None))

    def download(url, dest, log):
        from pathlib import Path
        Path(dest).write_bytes(b'audio')
        return True

    monkeypatch.setattr(mirror, 'download_asset', download)
    mirror.sync_mirror(log=lambda message: None)
    root = ET.parse(mirror.MIRROR_FEED_FILE)
    assert [i.findtext('title') for i in root.findall('channel/item')] == ['later', 'earlier']
    store.add_episode(episode('legacy', '2020-01-01T08:30:00'))
    stats = promote.promote_mirror(log=lambda message: None)
    assert stats['added'] == 2
    assert titles() == ['later', 'legacy', 'earlier']
    root = ET.parse(feed.FEED_FILE)
    assert [i.findtext('title') for i in root.findall('channel/item')] == titles()
    assert promote.promote_mirror(log=lambda message: None)['added'] == 0
