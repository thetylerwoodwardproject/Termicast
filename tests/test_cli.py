import cli
import pytest


@pytest.mark.parametrize('tty,no_color,term,expected', [
    (True, False, 'xterm', '\033[38;2;192;255;0mHello\033[0m'),
    (False, False, 'xterm', 'Hello'),
    (True, True, 'xterm', 'Hello'),
    (True, False, 'dumb', 'Hello'),
])
def test_color(monkeypatch, tty, no_color, term, expected):
    monkeypatch.setattr(cli.sys.stdout, 'isatty', lambda: tty)
    monkeypatch.delenv('NO_COLOR', raising=False)
    if no_color:
        monkeypatch.setenv('NO_COLOR', '')
    monkeypatch.setenv('TERM', term)
    assert cli.color('Hello') == expected


def test_date_display_uses_utc():
    assert cli.format_date('2020-01-01T10:00:00+02:00') == '2020-01-01 08:00 UTC'
