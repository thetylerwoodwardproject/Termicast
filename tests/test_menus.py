import io
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from rich.text import Text

import cli
import store


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture(autouse=True)
def ui_environment(monkeypatch, data_dir):
    for name in ('NO_COLOR', 'FORCE_COLOR', 'TERMICAST_REDUCED_MOTION'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('TERM', 'xterm')
    monkeypatch.setenv('COLUMNS', '80')

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, 9, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(cli, 'datetime', Clock)


def episode(title, date):
    return {'id': title + '-identifier', 'title': title,
            'filename': title + '.mp3', 'pubDate': date,
            'episodeNumber': 0, 'season': 1}


@pytest.mark.parametrize('answers,default,expected,invalid', [
    ([' 2 '], None, 'Beta', 0),
    ([''], 'Beta', 'Beta', 0),
    (['', '0', '4', '-1', 'word', '1.5', '3'], None, 'Gamma', 6),
    (['invalid', ''], 'Alpha', 'Alpha', 1),
])
def test_prompt_choice_numeric_default_and_retry(monkeypatch, capsys, answers, default,
                                                 expected, invalid):
    inputs = Mock(side_effect=answers)
    monkeypatch.setattr('builtins.input', inputs)
    choices = ['Alpha', 'Beta', 'Gamma']
    groups = {'Alpha': 'First', 'Beta': 'Second', 'Gamma': 'Second'}
    assert cli.prompt_choice('Choose', choices, default=default, groups=groups) == expected
    output = capsys.readouterr().out
    assert output.count('Invalid choice.') == invalid
    assert inputs.call_count == len(answers)
    assert output.count('Choose') == 1
    assert output.index('1. Alpha') < output.index('2. Beta') < output.index('3. Gamma')
    assert output.count('Second') == 1
    if default:
        assert default + ' *' in output


@pytest.mark.parametrize('answer,expected', [
    ('', [0, 1, 2]),
    ('   ', [0, 1, 2]),
    ('3 1 3', [2, 0, 2]),
    ('0 4 -1 nope 2 1.5', [1]),
    ('nope 0 4', []),
])
def test_prompt_checkbox_preserves_zero_based_indices(monkeypatch, capsys, answer, expected):
    choices = ['Alpha', 'Beta', 'Gamma']
    monkeypatch.setattr('builtins.input', Mock(return_value=answer))
    assert cli.prompt_checkbox('Select', choices) == expected
    assert choices == ['Alpha', 'Beta', 'Gamma']
    output = capsys.readouterr().out
    assert '1. Alpha' in output and '3. Gamma' in output
    assert 'Enter for all' in output


@pytest.mark.parametrize('feed_state', ['missing', 'file', 'directory'])
def test_main_dashboard_truthful_and_exit(monkeypatch, capsys, data_dir, feed_state):
    store.save({'show': {'title': '[red]Local show[/red]'}, 'episodes': [
        episode('past', '2026-01-01T10:00:00+02:00'),
        episode('now', '2026-01-01T09:00:00Z'),
        episode('future', '2026-01-01T08:30:00-01:00'),
        episode('bad', 'not-a-date'),
    ], 'mirror': {'sourceUrl': 'https://source.invalid/feed.xml'}})
    if feed_state == 'file':
        (data_dir / 'feed.xml').touch()
    elif feed_state == 'directory':
        (data_dir / 'feed.xml').mkdir()
    original = (data_dir / 'podcast.json').read_bytes()
    monkeypatch.setattr('builtins.input', Mock(return_value='8'))
    wizard = Mock(side_effect=AssertionError('Existing data must skip setup'))
    monkeypatch.setattr(cli, 'first_run_wizard', wizard)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    output = capsys.readouterr().out
    for expected in ('[red]Local show[/red]', 'Episodes: 4', 'Scheduled: 1',
                     'Mirror source: https://source.invalid/feed.xml', '8. Exit', 'Bye.'):
        assert expected in output
    assert 'Local feed.xml: ' + ('Present' if feed_state == 'file' else 'Missing') in output
    assert not any(word in output.lower() for word in ('healthy', 'online', 'reachable'))
    wizard.assert_not_called()
    assert (data_dir / 'podcast.json').read_bytes() == original


@pytest.mark.parametrize('number,handler', [
    (1, 'add_episode'), (2, 'edit_episode'), (3, 'delete_episode'),
    (4, 'list_episodes'), (5, 'setup_show'), (6, 'import_from_rss'),
    (7, 'mirror_menu'),
])
def test_main_dispatch_and_menu_group_kwargs(monkeypatch, number, handler):
    store.save({'show': {}, 'episodes': []})
    handlers = {}
    for name in ('add_episode', 'edit_episode', 'delete_episode', 'list_episodes',
                 'setup_show', 'import_from_rss', 'mirror_menu'):
        handlers[name] = Mock()
        monkeypatch.setattr(cli, name, handlers[name])
    monkeypatch.setattr('builtins.input', Mock(side_effect=[str(number), '8']))
    choice = Mock(wraps=cli.prompt_choice)
    monkeypatch.setattr(cli, 'prompt_choice', choice)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    for name, mock in handlers.items():
        assert mock.call_count == (1 if name == handler else 0)
    assert choice.call_count == 2
    for call in choice.call_args_list:
        assert call.args == ('CONTROL ROOM / Choose a number', [
            'Add episode', 'Edit episode', 'Delete episode', 'List episodes',
            'Edit show settings', 'Import from RSS feed', 'Mirror external feed', 'Exit'])
        assert call.kwargs == {'groups': {
            'Add episode': 'PUBLISH & MANAGE', 'Edit episode': 'PUBLISH & MANAGE',
            'Delete episode': 'PUBLISH & MANAGE', 'List episodes': 'PUBLISH & MANAGE',
            'Edit show settings': 'CONFIGURATION', 'Import from RSS feed': 'MIRROR & MIGRATION',
            'Mirror external feed': 'MIRROR & MIGRATION', 'Exit': 'SESSION'}}


def test_list_episodes_plain_utc_order_and_status(capsys):
    store.save({'show': {}, 'episodes': [
        episode('earlier', '2026-01-01T10:00:00+02:00'),
        episode('scheduled', '2026-01-01T08:30:00-01:00'),
        episode('legacy', '2026-01-01T08:30:00'),
        episode('boundary', '2026-01-01T09:00:00Z'),
    ]})
    cli.list_episodes()
    output = capsys.readouterr().out
    positions = [output.index('  ' + name + '\n')
                 for name in ('scheduled', 'boundary', 'legacy', 'earlier')]
    assert positions == sorted(positions)
    assert 'Published: 2026-01-01 09:30 UTC\n    Status: Scheduled' in output
    assert 'Published: 2026-01-01 09:00 UTC\n    Status: Published' in output
    assert 'Published: 2026-01-01 08:30 UTC' in output
    assert 'Published: 2026-01-01 08:00 UTC' in output
    assert output.count('Status: Published') == 3
    assert 'Number: 0\n    Season: 1' in output
    assert 'Filename: earlier.mp3\n    Id: earlier-' in output
    assert '4 episodes | Newest first | Dates in UTC' in output
    assert '\x1b' not in output and '\r' not in output


def test_list_episodes_empty(capsys):
    store.save({'show': {}, 'episodes': []})
    cli.list_episodes()
    output = capsys.readouterr().out
    assert 'No episodes yet.' in output
    assert 'Newest first' not in output


def test_list_episodes_invalid_date_status(capsys):
    store.save({'show': {}, 'episodes': [episode('broken', 'not-a-date')]})
    cli.list_episodes()
    output = capsys.readouterr().out
    assert 'broken' in output and 'Status: Invalid date' in output


@pytest.mark.parametrize('width', [40, 80])
def test_list_episodes_tty_literal_long_title(monkeypatch, width):
    monkeypatch.setenv('COLUMNS', str(width))
    monkeypatch.setattr(cli.sys, 'stdout', Terminal())
    title = '[red]A long episode title that must wrap without losing any words[/red]'
    row = episode(title, '2026-01-01T10:00:00Z')
    row.update(filename='recording.mp3', id='unique-identifier')
    store.save({'show': {}, 'episodes': [row]})
    cli.list_episodes()
    output = Text.from_ansi(cli.sys.stdout.getvalue()).plain
    lines = output.splitlines()
    assert any('Title' in line for line in lines), output
    header = next(line for line in lines if 'Title' in line)
    start, end = header.index('Title'), header.index('#')
    body = lines[lines.index(header) + 1:-1]
    rendered = ''.join(line[start:end].strip() for line in body)
    assert title.replace(' ', '') in rendered.replace(' ', '')
    assert ('Filename' in output) is (width == 80)
    assert ('unique-i' in output) is (width == 80)
    assert '1 episodes | Newest first | Dates in UTC' in output
    assert all(len(line) <= width for line in lines), output
