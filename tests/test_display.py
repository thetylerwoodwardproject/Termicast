import io
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from rich.console import Console
from rich.progress import DownloadColumn, MofNCompleteColumn, TransferSpeedColumn, TimeRemainingColumn

import display


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture(autouse=True)
def terminal_environment(monkeypatch):
    for name in ('NO_COLOR', 'TERMICAST_REDUCED_MOTION', 'FORCE_COLOR'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('TERM', 'xterm')
    monkeypatch.setenv('COLUMNS', '100')


def plain(value):
    from rich.text import Text
    return Text.from_ansi(value).plain


@pytest.mark.parametrize('tty,no_color,term,expected', [
    (True, False, 'xterm', '\033[31mhello\033[0m'),
    (False, False, 'xterm', 'hello'),
    (True, True, 'xterm', 'hello'),
    (True, False, 'dumb', 'hello'),
])
def test_color(monkeypatch, tty, no_color, term, expected):
    monkeypatch.setattr(display.sys, 'stdout', Terminal() if tty else io.StringIO())
    monkeypatch.delenv('NO_COLOR', raising=False)
    if no_color:
        monkeypatch.setenv('NO_COLOR', '')
    monkeypatch.setenv('TERM', term)
    assert display.color('hello', '31') == expected


@pytest.mark.parametrize('unit', ['bytes', 'items', ''])
def test_non_tty_progress(monkeypatch, unit):
    output = io.StringIO()
    monkeypatch.setattr(display.sys, 'stdout', output)
    progress = display.Progress(10, 'Assets', unit=unit)
    progress.update(5)
    assert output.getvalue() == ''
    progress.finish()
    progress.finish()
    assert output.getvalue() == 'Assets [' + '=' * 20 + '-' * 20 + '] 5/10' + (f' {unit}' if unit else '') + '\n'
    assert '\r' not in output.getvalue()


def test_tty_throttle_and_final(monkeypatch):
    output = Terminal()
    monkeypatch.setattr(display.sys, 'stdout', output)
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setenv('TERM', 'xterm')
    times = iter([0, 0.01, 0.11])
    monkeypatch.setattr(display, 'time', SimpleNamespace(monotonic=lambda: next(times)))
    progress = display.Progress(3, 'Files', unit='items')
    assert output.getvalue() == ''
    progress.update(1)
    first = output.getvalue()
    progress.update(2)
    assert output.getvalue() == first
    progress.update(3)
    progress.finish()
    assert '3/3' in plain(output.getvalue()).replace(' ', '')
    assert 'Files' in plain(output.getvalue())
    assert 'items' in plain(output.getvalue())
    assert not progress._progress.live.is_started
    assert '\033[?25h' in output.getvalue()
    assert plain(output.getvalue()).endswith('\n')


def test_custom_log_and_silence(monkeypatch):
    output = Terminal()
    monkeypatch.setattr(display.sys, 'stdout', output)
    messages = []
    for log in (messages.append, lambda message: None):
        progress = display.Progress(label='Unknown', log=log)
        progress.update(123)
        progress.finish()
    assert output.getvalue() == ''
    assert len(messages) == 1
    assert '123 bytes' in messages[0]
    assert '\r' not in messages[0]


def test_close_unfinished_tty(monkeypatch):
    output = Terminal()
    monkeypatch.setattr(display.sys, 'stdout', output)
    progress = display.Progress()
    progress.update(1)
    progress.close()
    progress.close()
    assert not progress._progress.live.is_started
    assert '\033[?25h' in output.getvalue()
    previous = output.getvalue()
    progress.finish()
    assert output.getvalue() == previous


@pytest.mark.parametrize('tty,term,no_color,reduced', [
    (True, 'xterm', False, False),
    (True, 'xterm', True, False),
    (True, 'dumb', False, False),
    (False, 'xterm', False, False),
    (True, 'xterm', False, True),
])
def test_console_capabilities(monkeypatch, tty, term, no_color, reduced):
    output = Terminal() if tty else io.StringIO()
    monkeypatch.setattr(display.sys, 'stdout', output)
    monkeypatch.setenv('TERM', term)
    if no_color:
        monkeypatch.setenv('NO_COLOR', '')
    if reduced:
        monkeypatch.setenv('TERMICAST_REDUCED_MOTION', '')
    assert display.terminal() is (tty and term != 'dumb')
    assert display.motion() is (tty and term != 'dumb' and not reduced)
    console = display.console()
    assert isinstance(console, Console)
    assert console is not display.console()
    console.print('123 True https://example.test')
    assert output.getvalue() == '123 True https://example.test\n'
    import re
    console.print('[red]literal[/red] 123', style='accent')
    text = output.getvalue()
    assert '[red]literal[/red] 123' in plain(text)
    assert bool(re.search(r'\x1b\[[0-9;]*m', text)) is (tty and term != 'dumb' and not no_color)
    replacement = io.StringIO()
    monkeypatch.setattr(display.sys, 'stdout', replacement)
    display.console().print('new stdout')
    assert replacement.getvalue() == 'new stdout\n'


def test_plain_menu_original_format(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(display.sys, 'stdout', output)
    display.menu('Choose [one]', ['Alpha', '[red]Beta'], default='Alpha')
    assert output.getvalue() == '\n  Choose [one]\n    1. Alpha *\n    2. [red]Beta\n'


@pytest.mark.parametrize('tty', [False, True])
def test_menu_groups_keep_order_and_literal_text(monkeypatch, tty):
    output = Terminal() if tty else io.StringIO()
    monkeypatch.setattr(display.sys, 'stdout', output)
    display.menu('[red]Menu[/red]', ['A', 'B', 'C', 'D', 'E'], default='C',
                 groups={'A': '[blue]Group', 'B': '[blue]Group',
                         'D': 'Other', 'E': '[blue]Group'})
    text = plain(output.getvalue())
    assert '[red]Menu[/red]' in text
    assert text.count('[blue]Group') == 2
    assert text.index('1. A') < text.index('2. B') < text.index('3. C *') < text.index('4. D') < text.index('5. E')


@pytest.mark.parametrize('tty', [False, True])
@pytest.mark.parametrize('exists', [False, True])
def test_dashboard_local_status(monkeypatch, tty, exists):
    output = Terminal() if tty else io.StringIO()
    monkeypatch.setattr(display.sys, 'stdout', output)
    display.dashboard('[red]Show[/red]', 7, 2, 'https://example.test/[feed]', exists)
    text = plain(output.getvalue())
    for expected in ('[red]Show[/red]', 'Episodes: 7', 'Scheduled: 2',
                     'https://example.test/[feed]', 'Local feed.xml: ' + ('Present' if exists else 'Missing')):
        assert expected in text
    assert not any(word in text.lower() for word in ('healthy', 'online', 'reachable'))


@pytest.mark.parametrize('tty', [False, True])
def test_section_literal(monkeypatch, tty):
    output = Terminal() if tty else io.StringIO()
    monkeypatch.setattr(display.sys, 'stdout', output)
    display.section('[bold]News[/bold]')
    assert '[bold]News[/bold]' in plain(output.getvalue())
    if not tty:
        assert output.getvalue() == '\n-- [bold]News[/bold] --\n'


@pytest.mark.parametrize('width', [40, 60, 61, 100])
def test_episode_table_responsive_literal(monkeypatch, width):
    output = Terminal()
    monkeypatch.setattr(display.sys, 'stdout', output)
    monkeypatch.setenv('COLUMNS', str(width))
    title = '[red]A long episode title that must wrap without losing words[/red]'
    display.episode_table([dict(title=title, id='unique-id', filename='recording.mp3',
                               published='2026-09-07', status='Scheduled', number=0, season=1)])
    text = plain(output.getvalue())
    assert all(len(line) <= width for line in text.splitlines())
    # Read the title column vertically to ensure wrapping never truncates it.
    lines = text.splitlines()
    header = next(line for line in lines if 'Title' in line)
    start, end = header.index('Title'), header.index('#')
    rendered_title = ''.join(line[start:end].strip() for line in lines[lines.index(header) + 1:])
    assert title.replace(' ', '') in rendered_title.replace(' ', '')
    if width < 80:
        assert 'Filename' not in text and 'unique-id' not in text
    else:
        assert 'Filename' in text and 'ID' in text


def test_plain_episode_table(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(display.sys, 'stdout', output)
    display.episode_table([dict(title='[red]Title', id='abc', filename='episode.mp3',
                               published='Tomorrow', status='Scheduled', number=0, season=None)])
    assert output.getvalue() == ('  [red]Title\n    Number: 0\n    Published: Tomorrow\n'
                                 '    Status: Scheduled\n    Filename: episode.mp3\n    Id: abc\n')


@pytest.mark.parametrize('fails', [False, True])
def test_busy_cleanup(monkeypatch, fails):
    output = Terminal()
    monkeypatch.setattr(display.sys, 'stdout', output)
    with pytest.raises(ValueError) if fails else nullcontext():
        with display.busy('[red]Working[/red]'):
            if fails:
                raise ValueError('failure')
    assert '\033[?25h' in output.getvalue()
    assert 'success' not in output.getvalue().lower()


@pytest.mark.parametrize('mode', ['plain', 'custom', 'reduced', 'dumb'])
def test_busy_static_or_custom(monkeypatch, mode):
    output = io.StringIO() if mode == 'plain' else Terminal()
    monkeypatch.setattr(display.sys, 'stdout', output)
    if mode == 'reduced':
        monkeypatch.setenv('TERMICAST_REDUCED_MOTION', '')
    if mode == 'dumb':
        monkeypatch.setenv('TERM', 'dumb')
    messages = []
    with pytest.raises(RuntimeError):
        with display.busy('[red]Working', log=messages.append if mode == 'custom' else print):
            raise RuntimeError('failure')
    assert output.getvalue() == ('' if mode == 'custom' else '[red]Working\n')
    assert messages == (['[red]Working'] if mode == 'custom' else [])


@pytest.mark.parametrize('no_color', [False, True])
def test_reduced_motion_progress(monkeypatch, no_color):
    output = Terminal()
    monkeypatch.setattr(display.sys, 'stdout', output)
    monkeypatch.setenv('TERMICAST_REDUCED_MOTION', '')
    if no_color:
        monkeypatch.setenv('NO_COLOR', '')
    progress = display.Progress(10, '[red]Files[/red] {literal}', unit='items')
    progress.update(3)
    progress.update(6)
    assert output.getvalue() == ''
    progress.finish()
    text = output.getvalue()
    assert '[red]Files[/red]' in plain(text)
    assert '{literal}' in plain(text)
    assert '6/10' in plain(text).replace(' ', '')
    assert '\r' not in text and '\033[?' not in text
    assert ('\033[' in text) is not no_color
    progress.finish()
    progress.close()
    assert output.getvalue() == text


@pytest.mark.parametrize('total', [None, 100])
def test_byte_progress_columns(monkeypatch, total):
    output = Terminal()
    monkeypatch.setattr(display.sys, 'stdout', output)
    progress = display.Progress(total, '[red]Download[/red]')
    progress.update(20)
    columns = progress._progress.columns
    for kind in (DownloadColumn, TransferSpeedColumn, TimeRemainingColumn):
        assert any(isinstance(column, kind) for column in columns)
    assert not any(isinstance(column, MofNCompleteColumn) for column in columns)
    progress.close()
    assert '[red]Download[/red]' in plain(output.getvalue())


@pytest.mark.parametrize('tty,reduced', [(False, False), (True, True), (True, False)])
def test_close_before_update_is_silent(monkeypatch, tty, reduced):
    output = Terminal() if tty else io.StringIO()
    monkeypatch.setattr(display.sys, 'stdout', output)
    if reduced:
        monkeypatch.setenv('TERMICAST_REDUCED_MOTION', '')
    progress = display.Progress(10)
    progress.close()
    progress.update(10)
    progress.finish()
    assert output.getvalue() == ''


def test_reduced_motion_error_is_silent(monkeypatch):
    output = Terminal()
    monkeypatch.setattr(display.sys, 'stdout', output)
    monkeypatch.setenv('TERMICAST_REDUCED_MOTION', '0')
    progress = display.Progress(10)
    progress.update(5)
    progress.close()
    progress.finish()
    assert output.getvalue() == ''


def test_dumb_progress_original_format(monkeypatch):
    output = Terminal()
    monkeypatch.setattr(display.sys, 'stdout', output)
    monkeypatch.setenv('TERM', 'dumb')
    progress = display.Progress(2, 'Files', unit='items')
    progress.update(1)
    assert output.getvalue() == ''
    progress.finish()
    assert output.getvalue() == 'Files [' + '=' * 20 + '-' * 20 + '] 1/2 items\n'


def test_no_color_progress_still_moves(monkeypatch):
    output = Terminal()
    monkeypatch.setattr(display.sys, 'stdout', output)
    monkeypatch.setenv('NO_COLOR', '')
    progress = display.Progress(2, 'Files', unit='items')
    progress.update(1)
    assert progress._progress.live.is_started
    assert 'Files' in plain(output.getvalue())
    progress.close()
    import re
    assert not re.search(r'\x1b\[[0-9;]*m', output.getvalue())


def test_finish_without_update_does_not_start_live(monkeypatch):
    output = Terminal()
    monkeypatch.setattr(display.sys, 'stdout', output)
    progress = display.Progress(2, 'Files', unit='items')
    progress.finish()
    assert '0/2' in plain(output.getvalue()).replace(' ', '')
    assert not progress._progress.live.is_started
    assert '\033[?' not in output.getvalue()


@pytest.mark.parametrize('width', [40, 80, 120])
@pytest.mark.parametrize('no_color', [False, True])
@pytest.mark.parametrize('reduced', [False, True])
def test_banner_terminal(monkeypatch, width, no_color, reduced):
    import re
    from rich.text import Text

    output = Terminal()
    monkeypatch.setattr(display.sys, 'stdout', output)
    monkeypatch.setenv('COLUMNS', str(width))
    if no_color:
        monkeypatch.setenv('NO_COLOR', '')
    if reduced:
        monkeypatch.setenv('TERMICAST_REDUCED_MOTION', '')
    monkeypatch.setattr(display, 'time', SimpleNamespace())
    renderer = display.console()
    renderables = []
    original_print = renderer.print

    def print_text(value):
        assert isinstance(value, Text)
        renderables.append(value)
        original_print(value)

    def unexpected(*args, **kwargs):
        pytest.fail('The banner must not clear, animate, or delay')

    monkeypatch.setattr(renderer, 'print', print_text)
    monkeypatch.setattr(renderer, 'clear', unexpected)
    monkeypatch.setattr(renderer, 'status', unexpected)
    monkeypatch.setattr(display, 'console', lambda: renderer)
    display.banner()
    assert len(renderables) == 1
    raw = output.getvalue()
    text = plain(raw)
    assert all(len(line) <= width for line in text.splitlines())
    assert all(32 <= ord(char) <= 126 for line in text.splitlines() for char in line)
    assert re.sub(r'\x1b\[[0-9;]*m', '', raw) == text
    # NO_COLOR suppresses color, not the shared theme's bold/dim attributes.
    assert bool(re.search(r'\x1b\[1;[0-9;]*m', raw)) is not no_color
    if no_color:
        assert not re.search(r'\x1b\[[0-9;]*3[0-9][;m]', raw)
    # The wordmark and tagline are centered within the terminal width.
    tagline = 'Your podcast. Your signal.'
    lines = text.splitlines()
    assert lines[-1] == tagline.center(width)
    if width < 80:
        assert lines == ['[ TERMICAST ]'.center(width), tagline.center(width)]
    else:
        assert lines[0] == '[ BROADCAST CONSOLE ]'
        bolt_lines = [
            ' _/',
            '/_ ',
            '  /',
            ' / ',
            '/  ',
        ]
        art_lines = [
            '#####  #####  ####   #   #  #####   ####   ###    ####  #####',
            '  #    #      #   #  ## ##    #    #      #   #  #        #  ',
            '  #    ####   ####   # # #    #    #      #####   ###     #  ',
            '  #    #      #  #   #   #    #    #      #   #      #    #  ',
            '  #    #####  #   #  #   #  #####   ####  #   #  ####     #  ',
        ]
        combined = [f'{bolt}  {letters}' for bolt, letters in zip(bolt_lines, art_lines)]
        centered = [line.center(width) for line in combined]
        assert lines[1:6] == centered
        pad = (width - len(combined[0])) // 2 + len(bolt_lines[0]) + 2
        # Five-column glyphs read T E R M I C A S T from left to right.
        rows = [row[pad:pad + len(art_lines[0])] for row in lines[1:6]]
        glyphs = ['\n'.join(row[i:i + 5].ljust(5) for row in rows)
                  for i in range(0, 63, 7)]
        assert glyphs == [
            '#####\n  #  \n  #  \n  #  \n  #  ',
            '#####\n#    \n#### \n#    \n#####',
            '#### \n#   #\n#### \n#  # \n#   #',
            '#   #\n## ##\n# # #\n#   #\n#   #',
            '#####\n  #  \n  #  \n  #  \n#####',
            ' ####\n#    \n#    \n#    \n ####',
            ' ### \n#   #\n#####\n#   #\n#   #',
            ' ####\n#    \n ### \n    #\n#### ',
            '#####\n  #  \n  #  \n  #  \n  #  ',
        ]


@pytest.mark.parametrize('width', [40, 80, 120])
@pytest.mark.parametrize('tty,term', [(False, 'xterm'), (False, 'dumb'), (True, 'dumb')])
@pytest.mark.parametrize('no_color', [False, True])
def test_banner_plain(monkeypatch, width, tty, term, no_color):
    output = Terminal() if tty else io.StringIO()
    monkeypatch.setattr(display.sys, 'stdout', output)
    monkeypatch.setenv('COLUMNS', str(width))
    monkeypatch.setenv('TERM', term)
    monkeypatch.setenv('FORCE_COLOR', '1')
    if no_color:
        monkeypatch.setenv('NO_COLOR', '')
    display.banner()
    assert output.getvalue() == 'TERMICAST - Your podcast. Your signal.\n'
