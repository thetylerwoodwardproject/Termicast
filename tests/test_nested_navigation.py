from copy import deepcopy
from unittest.mock import Mock

import pytest

import cli


@pytest.fixture
def navigation_input(monkeypatch):
    # Supply the shared input contract while its implementation is developed
    # separately; all menus and prompt functions under test remain real.
    if not hasattr(cli, 'CancelAction'):
        class CancelAction(Exception):
            pass
        monkeypatch.setattr(cli, 'CancelAction', CancelAction, raising=False)

    def install(answers):
        answers = iter(answers)

        def read_input(message):
            answer = next(answers)
            if answer is KeyboardInterrupt:
                raise KeyboardInterrupt
            if answer in ('/back', '/cancel') and not hasattr(cli, 'read_input'):
                raise cli.CancelAction
            return answer

        inputs = Mock(side_effect=read_input)
        monkeypatch.setattr('builtins.input', inputs)
        return inputs

    return install


@pytest.fixture(params=['/back', '/cancel', KeyboardInterrupt])
def cancel(request):
    return request.param


@pytest.fixture(params=['chapters', 'persons'])
def manager(request):
    if request.param == 'chapters':
        return (cli.manage_chapters,
                [{'startTime': 0, 'title': 'Original', 'url': ''}],
                ['1', '10', 'Completed', '', '', ''], '4', '3')
    return (cli.manage_persons, [{'name': 'Original', 'role': 'host'}],
            ['1', 'Completed', '', '', '', ''], '3', '2')


def test_manager_cancel_discards_completed_add_and_delete(
        navigation_input, manager, cancel):
    manage, existing, add, done, delete = manager
    original = deepcopy(existing)
    inputs = navigation_input([*add, delete, '1', cancel])
    assert manage(existing) is existing
    assert existing == original
    assert inputs.call_count == len(add) + 3


@pytest.mark.parametrize('existing', [None, []])
def test_empty_manager_cancel(navigation_input, manager, cancel, existing):
    manage = manager[0]
    navigation_input([cancel])
    result = manage(existing)
    assert result == []
    if existing is not None:
        assert result is existing


@pytest.mark.parametrize('field', range(5))
def test_cancel_each_add_field_keeps_completed_draft(
        navigation_input, manager, cancel, field):
    manage, existing, add, done, delete = manager
    original = deepcopy(existing)
    answers = [*add, *add[:field + 1], cancel, done]
    inputs = navigation_input(answers)
    result = manage(existing)
    assert len(result) == 2
    assert result[1].get('title', result[1].get('name')) == 'Completed'
    assert existing == original
    assert inputs.call_count == len(answers)


def test_cancel_delete_selection_keeps_completed_draft(
        navigation_input, manager, cancel):
    manage, existing, add, done, delete = manager
    original = deepcopy(existing)
    navigation_input([*add, delete, cancel, done])
    assert len(manage(existing)) == 2
    assert existing == original


@pytest.mark.parametrize('prefix', [
    ['2'], ['2', '1'], ['2', '1', '20'],
    ['2', '1', '20', 'Unfinished'],
    ['2', '1', '20', 'Unfinished', 'https://example.com'],
    ['2', '1', '20', 'Unfinished', '', ''],
])
def test_cancel_chapter_edit_preserves_completed_edit(
        navigation_input, cancel, prefix):
    existing = [{'startTime': 0, 'title': 'Original', 'toc': True}]
    original = deepcopy(existing)
    navigation_input(['2', '1', '10', 'Completed', '', '', '',
                      *prefix, cancel, '4'])
    assert cli.manage_chapters(existing) == [
        {'startTime': 10, 'title': 'Completed', 'toc': True}]
    assert existing == original


def test_manager_cancel_discards_completed_chapter_edit(navigation_input, cancel):
    existing = [{'startTime': 0, 'title': 'Original', 'url': ''}]
    original = deepcopy(existing)
    navigation_input(['2', '1', '10', 'Changed', '', '', '', cancel])
    assert cli.manage_chapters(existing) is existing
    assert existing == original


@pytest.fixture
def episode_edit(monkeypatch, data_dir):
    (data_dir / 'media' / 'audio.mp3').touch()
    episodes = [
        {'id': str(i), 'title': 'Episode ' + str(i),
         'pubDate': '2026-01-01T00:00:00+00:00', 'filename': 'audio.mp3',
         'description': 'Original description',
         'chapters': [{'startTime': 0, 'title': 'Original'}],
         'persons': [{'name': 'Original', 'role': 'host'}],
         'soundbite': {'startTime': 1, 'duration': 2},
         'location': {'name': 'Original'}}
        for i in range(2)
    ]
    monkeypatch.setattr(cli.store, 'get_episodes', lambda: episodes)
    update = Mock()
    regenerate = Mock()
    monkeypatch.setattr(cli.store, 'update_episode', update)
    monkeypatch.setattr(cli, 'regenerate', regenerate)
    return episodes, update, regenerate


def test_section_cancel_returns_to_episode_selection(
        navigation_input, episode_edit, cancel):
    episodes, update, regenerate = episode_edit
    navigation_input(['1', cancel, '2', '6', 'new.vtt'])
    cli.edit_episode()
    update.assert_called_once_with('1', {'transcript': 'new.vtt'})
    regenerate.assert_called_once_with()


@pytest.mark.parametrize('section,fields', [
    ('1', ['Changed', '', '', '', '', '', '', '', '', '', '', '', '']),
    ('4', ['n', '10', '5', 'Changed']),
    ('5', ['n', 'Changed', 'geo:1,2', 'R123']),
    ('6', ['new.vtt']),
])
def test_cancel_each_section_field_has_no_unfinished_writes(
        navigation_input, episode_edit, cancel, section, fields):
    episodes, update, regenerate = episode_edit
    original = deepcopy(episodes)
    for field in range(len(fields)):
        update.reset_mock()
        regenerate.reset_mock()
        answers = ['1', section, *fields[:field], cancel, '6', 'finished.vtt']
        inputs = navigation_input(answers)
        cli.edit_episode()
        update.assert_called_once_with('0', {'transcript': 'finished.vtt'})
        regenerate.assert_called_once_with()
        assert episodes == original
        assert inputs.call_count == len(answers)


@pytest.mark.parametrize('section,add', [
    ('2', ['1', '10', 'Completed', '', '', '']),
    ('3', ['1', 'Completed', '', '', '', '']),
])
@pytest.mark.parametrize('state', ['existing', 'empty', 'missing', 'null'])
def test_episode_manager_cancel_returns_to_sections_without_writing(
        navigation_input, episode_edit, cancel, section, add, state):
    episodes, update, regenerate = episode_edit
    key = 'chapters' if section == '2' else 'persons'
    if state == 'missing':
        episodes[0].pop(key)
    elif state != 'existing':
        episodes[0][key] = [] if state == 'empty' else None
    original = deepcopy(episodes)
    navigation_input(['1', section, *add, cancel, '6', 'finished.vtt'])
    cli.edit_episode()
    update.assert_called_once_with('0', {'transcript': 'finished.vtt'})
    regenerate.assert_called_once_with()
    assert episodes == original


@pytest.mark.parametrize('section,answers,expected', [
    ('1', ['Changed', '', '', '', '', '', '', '', '', '', '', '', ''],
     {'title': 'Changed'}),
    ('2', ['3', '1', '2'], {'chapters': None}),
    ('3', ['2', '1', '2'], {'persons': None}),
    ('4', ['y'], {'soundbite': None}),
    ('5', ['y'], {'location': None}),
    ('6', ['new.vtt'], {'transcript': 'new.vtt'}),
])
def test_successful_section_edit_writes_once_and_returns(
        navigation_input, episode_edit, section, answers, expected):
    episodes, update, regenerate = episode_edit
    original = deepcopy(episodes)
    inputs = navigation_input(['1', section, *answers])
    cli.edit_episode()
    update.assert_called_once()
    assert update.call_args.args[0] == '0'
    assert update.call_args.args[1].items() >= expected.items()
    regenerate.assert_called_once_with()
    assert episodes == original
    assert inputs.call_count == len(answers) + 2


def test_episode_selection_cancel_propagates_to_caller(
        navigation_input, episode_edit, cancel):
    episodes, update, regenerate = episode_edit
    navigation_input([cancel])
    with pytest.raises((cli.CancelAction, KeyboardInterrupt)):
        cli.edit_episode()
    update.assert_not_called()
    regenerate.assert_not_called()
