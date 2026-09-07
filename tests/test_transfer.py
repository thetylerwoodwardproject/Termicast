from unittest.mock import Mock
from urllib.parse import quote

import pytest
import requests

import transfer


class Response:
    def __init__(self, chunks=(b'new',), headers=None, error=None):
        self.chunks = chunks
        self.headers = headers or {}
        self.error = error
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def raise_for_status(self):
        if self.error:
            raise self.error

    def iter_content(self, chunk_size):
        assert chunk_size == 65536
        for chunk in self.chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    get = Mock(side_effect=AssertionError('unexpected network request'))
    monkeypatch.setattr(transfer.requests, 'get', get)
    monkeypatch.setattr(transfer.time, 'sleep', Mock())
    return get


def test_success_replaces_existing_and_uses_unique_temp(tmp_path, no_network, monkeypatch):
    dest = tmp_path / 'asset'
    dest.write_bytes(b'old')
    unrelated = tmp_path / 'asset.part'
    unrelated.write_bytes(b'other download')
    response = Response((b'', b'new'), {'Content-Length': '3'})
    no_network.side_effect = [response]
    replace = transfer.os.replace

    def inspect_replace(source, target):
        assert dest.read_bytes() == b'old'
        assert str(source) not in (str(dest), str(unrelated))
        assert response.closed
        replace(source, target)

    monkeypatch.setattr(transfer.os, 'replace', inspect_replace)
    logs = []
    assert transfer.download('https://example.com/a', dest, logs.append, 'Audio')
    assert dest.read_bytes() == b'new'
    assert dest.stat().st_mode & 0o777 == 0o644
    assert unrelated.read_bytes() == b'other download'
    assert len(list(tmp_path.iterdir())) == 2
    assert 'Audio' in logs[-1]
    kwargs = no_network.call_args.kwargs
    assert kwargs['stream'] and kwargs['timeout'] == 60
    assert kwargs['headers']['User-Agent'] == transfer.USER_AGENT


def test_permission_failure_does_not_retry(tmp_path, no_network, monkeypatch):
    no_network.side_effect = [Response()]
    monkeypatch.setattr(transfer.tempfile, 'NamedTemporaryFile',
                        Mock(side_effect=PermissionError('not writable')))
    logs = []
    assert not transfer.download('https://op3.dev/e/example.com/a', tmp_path / 'asset', logs.append)
    assert no_network.call_count == 1
    transfer.time.sleep.assert_not_called()
    assert 'Cannot write download' in logs[-1]


def test_stream_retry(tmp_path, no_network):
    broken = Response((b'partial', requests.exceptions.ChunkedEncodingError('broken')))
    good = Response()
    no_network.side_effect = [broken, good]
    dest = tmp_path / 'nested' / 'asset'
    assert transfer.download('https://example.com/a', dest, lambda _: None)
    assert dest.read_bytes() == b'new'
    assert broken.closed and good.closed
    transfer.time.sleep.assert_called_once_with(0.5)
    assert list(dest.parent.iterdir()) == [dest]


@pytest.mark.parametrize('response', [
    Response(()), Response(headers={'Content-Length': '4'}),
    Response(headers={'Content-Length': '2'}),
    Response(headers={'Content-Length': 'invalid'}),
    Response(headers={'Content-Length': '-1'}),
    Response(error=requests.HTTPError('403')),
])
def test_failure_preserves_destination(tmp_path, no_network, response):
    dest = tmp_path / 'asset'
    dest.write_bytes(b'old')
    no_network.side_effect = [response]
    assert not transfer.download('https://example.com/a', dest, lambda _: None, attempts=1)
    assert dest.read_bytes() == b'old'
    assert list(tmp_path.iterdir()) == [dest]
    assert response.closed


def test_encoded_length_not_compared(tmp_path, no_network):
    no_network.side_effect = [Response(headers={'Content-Length': '99', 'Content-Encoding': 'gzip'})]
    assert transfer.download('https://example.com/a', tmp_path / 'asset', lambda _: None)


@pytest.mark.parametrize('interrupt', [KeyboardInterrupt(), SystemExit()])
def test_interrupt_cleanup(tmp_path, no_network, interrupt):
    dest = tmp_path / 'asset'
    dest.write_bytes(b'old')
    response = Response((b'partial', interrupt))
    no_network.side_effect = [response]
    with pytest.raises(type(interrupt)):
        transfer.download('https://example.com/a', dest, lambda _: None)
    assert response.closed
    assert dest.read_bytes() == b'old'
    assert list(tmp_path.iterdir()) == [dest]
    transfer.time.sleep.assert_not_called()


@pytest.mark.parametrize('path', [
    'e/content.example.com/audio.mp3',
    'e/https://content.example.com/audio.mp3',
    'e,pg=abc/https://content.example.com/audio.mp3',
    'e,pg=abc/content.example.com/audio.mp3',
])
def test_op3_fallback(tmp_path, no_network, path):
    url = 'https://op3.dev/' + path + '?token=a%2Fb&x=2'
    no_network.side_effect = [requests.ConnectionError('OP3 unavailable'), Response()]
    logs = []
    assert transfer.download(url, tmp_path / 'asset', logs.append, attempts=1)
    assert [call.args[0] for call in no_network.call_args_list] == [
        url, 'https://content.example.com/audio.mp3?token=a%2Fb&x=2']
    assert any('direct origin' in line for line in logs)


def test_success_does_not_bypass_op3(tmp_path, no_network):
    no_network.side_effect = [Response()]
    assert transfer.download('https://op3.dev/e/content.example/a', tmp_path / 'asset', lambda _: None)
    assert no_network.call_count == 1


@pytest.mark.parametrize('url', [
    'https://op3.dev.evil/e/content.example/a',
    'https://evil/op3.dev/e/content.example/a',
    'https://op3.dev@evil/e/content.example/a',
    'https://user@op3.dev/e/content.example/a',
    'https://op3.dev:123/e/content.example/a',
    'https://op3.dev/else/content.example/a',
    'https://op3.dev/e/', 'https://op3.dev/e//evil/a',
    'https://op3.dev/e/https://user@evil/a',
    'https://op3.dev/e/https://evil:bad/a',
    'https://op3.dev/e/https://op3.dev/e/evil/a',
    'https://op3.dev/e/https://evil\\host/a',
    'https://op3.dev//e/content.example/a',
    'https://op3.dev/e/ftp://content.example/a',
])
def test_no_unsafe_fallback(tmp_path, no_network, url):
    no_network.side_effect = requests.ConnectionError('failed')
    assert not transfer.download(url, tmp_path / 'asset', lambda _: None, attempts=1)
    assert no_network.call_count == 1
    assert not list(tmp_path.iterdir())


def test_bounded_backoff(tmp_path, no_network):
    no_network.side_effect = requests.Timeout('timeout')
    assert not transfer.download('https://example.com/a', tmp_path / 'asset', lambda _: None, attempts=7)
    assert no_network.call_count == 7
    assert [call.args[0] for call in transfer.time.sleep.call_args_list] == [0.5, 1, 2, 4, 4, 4]


def test_op3_and_origin_each_get_retry_budget(tmp_path, no_network):
    url = 'https://op3.dev/e/http://example.com/a'
    no_network.side_effect = [requests.Timeout('failed')] * 3 + [Response()]
    assert transfer.download(url, tmp_path / 'asset', lambda _: None, attempts=2)
    assert [call.args[0] for call in no_network.call_args_list] == [
        url, url, 'http://example.com/a', 'http://example.com/a']


def test_zero_attempts(tmp_path, no_network):
    assert not transfer.download('https://example.com/a', tmp_path / 'asset', attempts=0)
    no_network.assert_not_called()


def test_silent_logger(tmp_path, no_network, capsys):
    no_network.side_effect = [requests.ConnectionError('failed'), Response()]
    assert transfer.download('https://op3.dev/e/example.com/a', tmp_path / 'asset', lambda _: None, attempts=1)
    assert capsys.readouterr() == ('', '')


def test_replace_failure_cleanup(tmp_path, no_network, monkeypatch):
    dest = tmp_path / 'asset'
    dest.write_bytes(b'old')
    no_network.side_effect = [Response()]
    monkeypatch.setattr(transfer.os, 'replace', Mock(side_effect=OSError('denied')))
    assert not transfer.download('https://example.com/a', dest, lambda _: None, attempts=1)
    assert dest.read_bytes() == b'old'
    assert list(tmp_path.iterdir()) == [dest]


STATIC = 'https://rsscom.pdn.tritondigital.com/show/audio.mp3?Signature=signed%2Bvalue&Expires=123'
VARIANT = 'https://rsscom.pdn.tritondigital.com/v1/variant/id.mp3?fallback_url='


def test_triton_stream_failure_uses_advertised_fallback(tmp_path, no_network):
    broken = Response((b'partial', requests.exceptions.ChunkedEncodingError('IncompleteRead')))
    broken.url = VARIANT + quote(STATIC, safe='')
    no_network.side_effect = [broken, Response(headers={'Content-Length': '3'})]
    dest = tmp_path / 'audio.mp3'
    logs = []
    url = 'https://op3.dev/e/content.rss.com/episode.mp3'
    assert transfer.download(url, dest, logs.append)
    assert [call.args[0] for call in no_network.call_args_list] == [url, STATIC]
    assert dest.read_bytes() == b'new'
    assert broken.closed
    assert any('host-advertised static audio fallback' in line for line in logs)
    assert list(tmp_path.iterdir()) == [dest]


def test_triton_success_does_not_use_fallback(tmp_path, no_network):
    response = Response()
    response.url = VARIANT + quote(STATIC, safe='')
    no_network.side_effect = [response]
    assert transfer.download('https://source.test/audio', tmp_path / 'asset', lambda _: None)
    assert no_network.call_count == 1


def test_triton_fallback_is_bounded_and_validated(tmp_path, no_network):
    response = Response(headers={'Content-Length': '20'})
    response.url = VARIANT + quote(STATIC, safe='')
    no_network.side_effect = [response] * 5
    dest = tmp_path / 'asset'
    dest.write_bytes(b'original')
    assert not transfer.download('https://op3.dev/e/content.rss.com/a', dest, lambda _: None, attempts=2)
    assert no_network.call_count == 5
    assert dest.read_bytes() == b'original'
    assert list(tmp_path.iterdir()) == [dest]


@pytest.mark.parametrize('target', [
    'https://evil.test/audio.mp3',
    'http://rsscom.pdn.tritondigital.com/audio.mp3',
    'https://user@rsscom.pdn.tritondigital.com/audio.mp3',
    'https://rsscom.pdn.tritondigital.com:444/audio.mp3',
    'https://rsscom.pdn.tritondigital.com/v1/variant/other.mp3',
    'https://rsscom.pdn.tritondigital.com/audio.html',
    'https://rsscom.pdn.tritondigital.com/audio\\bad.mp3',
    '',
])
def test_triton_rejects_unsafe_fallback(target):
    assert transfer._triton_fallback(VARIANT + quote(target, safe='')) is None


def test_triton_requires_known_variant_host():
    assert transfer._triton_fallback(VARIANT.replace('rsscom.pdn.tritondigital.com', 'evil.test')
                                     + quote(STATIC, safe='')) is None
