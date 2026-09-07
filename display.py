"""Shared broadcast-console presentation and progress reporting."""
import os
import sys
import time
from contextlib import contextmanager

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import (BarColumn, DownloadColumn, MofNCompleteColumn,
                           Progress as RichProgress, SpinnerColumn, TextColumn,
                           TimeRemainingColumn, TransferSpeedColumn)
from rich.rule import Rule
from rich.table import Column, Table
from rich.text import Text
from rich.theme import Theme


THEME = Theme({'broadcast': 'bold cyan', 'accent': 'cyan', 'muted': 'dim',
               'warning': 'yellow'})


def terminal():
    return bool(sys.stdout.isatty() and os.environ.get('TERM') != 'dumb')


def motion():
    return terminal() and 'TERMICAST_REDUCED_MOTION' not in os.environ


def console():
    """Create a console bound to the current stdout, not an import-time stream."""
    return Console(file=sys.stdout, theme=THEME, markup=False, highlight=False,
                   force_terminal=terminal(),
                   no_color='NO_COLOR' in os.environ or os.environ.get('TERM') == 'dumb')


def banner():
    """Print a static wordmark without clearing or taking over the terminal."""
    output = console()
    tagline = 'Your podcast. Your signal.'
    if not terminal():
        output.print(Text(f'TERMICAST - {tagline}'))
        return

    body = Text()
    if output.width >= 80:
        body.append('[ BROADCAST CONSOLE ]\n', style='muted')
        body.append(
            '#####  #####  ####   #   #  #####   ####   ###    ####  #####\n'
            '  #    #      #   #  ## ##    #    #      #   #  #        #  \n'
            '  #    ####   ####   # # #    #    #      #####   ###     #  \n'
            '  #    #      #  #   #   #    #    #      #   #      #    #  \n'
            '  #    #####  #   #  #   #  #####   ####  #   #  ####     #  \n',
            style='broadcast')
    else:
        body.append('[ TERMICAST ]\n', style='broadcast')
    body.append(tagline, style='muted')
    output.print(body)


def section(title):
    if terminal():
        console().print(Rule(Text(str(title), style='broadcast'), style='accent'))
    else:
        print(f'\n-- {title} --')


def menu(message, choices, default=None, groups=None):
    """Render choices without reading input or changing their numbering."""
    body = Text()
    previous = None
    for number, choice in enumerate(choices, 1):
        group = groups.get(choice) if groups else None
        if group != previous and group is not None:
            body.append(f'  {group}\n', style='broadcast')
        previous = group
        body.append(f'    {number}.', style='accent')
        body.append(f' {choice}' + (' *' if choice == default else '') + '\n')
    if terminal():
        console().print(Panel(body[:-1], title=Text(str(message)),
                              border_style='accent', box=box.ROUNDED))
    else:
        print(f'\n  {message}')
        print(body.plain, end='')


def dashboard(show_title, episode_count, scheduled_count, mirror_source, feed_exists):
    """Report local configuration and files; do not infer remote availability."""
    body = Text(str(show_title) if show_title else 'Show not configured', style='broadcast')
    body.append(f'\nEpisodes: {episode_count}\nScheduled: {scheduled_count}', style='none')
    body.append(f'\nMirror source: {mirror_source or "Not configured"}', style='none')
    body.append('\nLocal feed.xml: ' + ('Present' if feed_exists else 'Missing'), style='none')
    if terminal():
        console().print(Panel(body, title=Text('TERMICAST'), border_style='accent'))
    else:
        print(body.plain)


def episode_table(rows):
    if not terminal():
        for row in rows:
            print(f'  {row.get("title", "")}')
            for key in ('number', 'season', 'published', 'status', 'filename', 'id'):
                value = row.get(key)
                if value is not None and value != '':
                    print(f'    {key.capitalize()}: {value}')
        return
    output = console()
    narrow = output.width < 80
    table = Table(box=box.SIMPLE, border_style='accent', header_style='broadcast',
                  padding=(0, 1) if output.width > 100 else (0, 0), expand=True)
    columns = [('title', 'Title'), ('number', '#'), ('season', 'Season'),
               ('published', 'Published'), ('status', 'Status')]
    if not narrow:
        columns.extend([('filename', 'Filename'), ('id', 'ID')])
    for key, heading in columns:
        table.add_column(heading, ratio=3 if key == 'title' else 1,
                         min_width=(12 if key == 'title' else len(heading)) if not narrow else None,
                         overflow='fold', no_wrap=False)
    for row in rows:
        table.add_row(*(Text(str(row[key]) if row.get(key) is not None else '',
                            style='warning' if key == 'status' and row.get(key) in ('Scheduled', 'Invalid date') else '')
                        for key, _ in columns))
    output.print(table)


@contextmanager
def busy(message, log=print):
    if log is print and motion():
        with console().status(Text(str(message)), spinner='dots', spinner_style='accent'):
            yield
    else:
        log(message)
        yield


def color(text, code='36'):
    if not sys.stdout.isatty() or 'NO_COLOR' in os.environ or os.environ.get('TERM') == 'dumb':
        return text
    return f'\033[{code}m{text}\033[0m'


class Progress:
    """Rich TTY progress; update() takes an absolute byte/item count.

    Only the default print logger redraws a TTY (at most every 0.1 seconds).
    Other loggers and non-TTY output receive one final line, via finish().
    Reduced motion renders only at finish(). close() never claims success.
    """

    def __init__(self, total=None, label='', log=print, unit='bytes'):
        self.total = total if total is not None and total > 0 else None
        self.label = label
        self.log = log
        self.unit = unit
        self.current = 0
        self._tty = log is print and terminal()
        self._last = None
        self._closed = False
        self._progress = None
        self._task = None

    def _renderer(self):
        if self._progress is None:
            columns = []
            if motion():
                columns.append(SpinnerColumn(style='accent'))
            columns.extend([TextColumn('{task.description}', markup=False,
                                       table_column=Column(max_width=32, overflow='ellipsis')),
                            BarColumn(bar_width=None, complete_style='accent',
                                      finished_style='accent', pulse_style='accent')])
            if self.unit == 'bytes':
                columns.extend([DownloadColumn(), TransferSpeedColumn(), TimeRemainingColumn()])
            else:
                columns.extend([MofNCompleteColumn(),
                                TextColumn('{task.fields[unit]}', markup=False)])
            self._progress = RichProgress(*columns, console=console(), auto_refresh=False)
            self._task = self._progress.add_task(str(self.label), total=self.total,
                                                 completed=self.current, unit=str(self.unit))
        return self._progress

    def _line(self):
        fraction = min(1, max(0, self.current / self.total)) if self.total else 0
        filled = int(40 * fraction)
        bar = color('[' + '=' * filled + '-' * (40 - filled) + ']')
        amount = str(self.current)
        if self.total:
            amount += f'/{self.total}'
        suffix = f' {self.unit}' if self.unit else ''
        prefix = f'{self.label} ' if self.label else ''
        return f'{prefix}{bar} {amount}{suffix}'

    def update(self, current):
        if self._closed:
            return
        self.current = current
        now = time.monotonic()
        if self._tty and motion() and (self._last is None or now - self._last >= 0.1):
            renderer = self._renderer()
            renderer.update(self._task, completed=self.current)
            if self._last is None:
                renderer.start()
            else:
                renderer.refresh()
            self._last = now

    def finish(self):
        if self._closed:
            return
        if self._tty:
            renderer = self._renderer()
            renderer.update(self._task, completed=self.current)
            if self._last is not None:
                renderer.stop()
            else:
                renderer.console.print(renderer.get_renderable())
        else:
            self.log(self._line())
        self._closed = True

    def close(self):
        if not self._closed and self._progress is not None and self._last is not None:
            self._progress.stop()
        self._closed = True
