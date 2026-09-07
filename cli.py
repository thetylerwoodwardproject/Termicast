#!/usr/bin/env python3
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
import xml.etree.ElementTree as ET

import store
import feed as feedgen
from display import color, Progress
from display import banner, busy, console, dashboard, episode_table, menu, section
from transfer import download

MEDIA_DIR = os.path.join(store.BASE_DIR, 'media')
os.makedirs(MEDIA_DIR, exist_ok=True)


# ─── Prompt helpers ──────────────────────────────────────────────────────────

class CancelAction(Exception):
    """Return to the nearest menu without accepting the current prompt."""


def read_input(message):
    console().print('/back or /cancel: return | Ctrl+C: cancel', style='muted')
    try:
        value = input(color(message)).strip()
    except KeyboardInterrupt:
        raise CancelAction() from None
    if value.lower() in ('/back', '/cancel'):
        raise CancelAction()
    if value.lower() in ('//back', '//cancel'):
        return value[1:]
    return value


def prompt(message, default=None, required=False):
    if default:
        msg = f'  {message} [{default}]: '
    else:
        msg = f'  {message}: '
    while True:
        val = read_input(msg)
        if not val and default is not None:
            return default
        if not val and required:
            print(color('    Required.', '33'))
            continue
        return val or None


def prompt_bool(message, default=True):
    yn = 'Y/n' if default else 'y/N'
    val = read_input(f'  {message} [{yn}]: ').lower()
    if not val:
        return default
    return val in ('y', 'yes')


def prompt_choice(message, choices, default=None, groups=None):
    menu(message, choices, default, groups)
    while True:
        val = read_input('  Choice: ')
        if not val and default:
            return default
        if val.isdigit() and 1 <= int(val) <= len(choices):
            return choices[int(val) - 1]
        print(color('    Invalid choice.', '33'))


def prompt_checkbox(message, choices):
    menu(message, choices)
    print('  Enter numbers separated by spaces (e.g. 1 3 5), or Enter for all:')
    val = read_input('  > ')
    if not val:
        return list(range(len(choices)))
    try:
        return [int(x) - 1 for x in val.split() if x.isdigit() and 1 <= int(x) <= len(choices)]
    except Exception:
        return list(range(len(choices)))


def parse_time_to_seconds(s):
    if not s:
        return 0
    s = s.strip()
    try:
        if ':' in s:
            parts = s.split(':')
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            if len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
        return float(s)
    except Exception:
        return 0


def format_date(date_str):
    if not date_str:
        return 'not set'
    try:
        return store.episode_date({'pubDate': date_str}).strftime('%Y-%m-%d %H:%M UTC')
    except Exception:
        return date_str


def filesize(filepath):
    try:
        return os.path.getsize(filepath)
    except Exception:
        return 0


def regenerate():
    with busy('Updating local feed.xml...'):
        live, scheduled = feedgen.write_feed()
    print(color('  feed.xml updated', '32') + f'  ({live} live', end='')
    if scheduled:
        print(f', {scheduled} scheduled', end='')
    print(')\n')


def divider(title=''):
    section(title)


def parse_pubdate_input(raw):
    """
    Parse a date string entered by the user and return a UTC ISO string.
    Interprets naive datetimes (no timezone specified) as local server time,
    then converts to UTC so storage is always consistent.
    """
    import time as _time
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        # Treat as local server time, convert to UTC
        local_ts = dt.timestamp()  # uses server's local timezone
        dt = datetime.fromtimestamp(local_ts, tz=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


# ─── Show settings ───────────────────────────────────────────────────────────

def setup_show():
    divider('Show Settings')
    show = store.get_show()

    updates = {}
    updates['title'] = prompt('Show title', show.get('title'), required=True)
    updates['description'] = prompt('Description', show.get('description'), required=True)
    updates['author'] = prompt('Author name', show.get('author'), required=True)
    updates['email'] = prompt('Contact email', show.get('email'))
    updates['artwork'] = prompt('Artwork URL', show.get('artwork'))
    updates['link'] = prompt('Show website', show.get('link', ''))
    updates['baseUrl'] = prompt('Base URL for media (e.g. https://audio.example.com)', show.get('baseUrl', ''))
    updates['op3Prefix'] = prompt('OP3 prefix', show.get('op3Prefix', 'https://op3.dev/e/'))
    updates['category'] = prompt('Apple Podcasts category (e.g. Technology)', show.get('category'))
    updates['subcategory'] = prompt('Subcategory (optional)', show.get('subcategory'))
    updates['language'] = prompt('Language code', show.get('language', 'en'))
    updates['explicit'] = prompt_bool('Explicit content?', show.get('explicit', False))

    if not show.get('guid'):
        updates['guid'] = str(uuid.uuid4())

    # Remove None values so we don't wipe existing data
    updates = {k: v for k, v in updates.items() if v is not None}
    store.save_show(updates)
    print('\n  Show settings saved.')
    regenerate()


# ─── Chapters ────────────────────────────────────────────────────────────────

def manage_chapters(existing=None):
    chapters = list(existing or [])

    while True:
        divider('Chapters')
        if not chapters:
            print('  No chapters yet.\n')
        else:
            for i, ch in enumerate(chapters, 1):
                img = ' [img]' if ch.get('img') else ''
                url = ' [url]' if ch.get('url') else ''
                print(f'  {i}. [{ch["startTime"]}s] {ch["title"]}{img}{url}')
            print()

        choices = ['Add chapter']
        if chapters:
            choices += ['Edit chapter', 'Delete chapter']
        choices.append('Done')

        try:
            action = prompt_choice('Chapters', choices)
        except (CancelAction, KeyboardInterrupt):
            return existing if existing is not None else []

        if action == 'Done':
            break

        try:
            if action == 'Add chapter':
                start_raw = prompt('Start time (seconds or HH:MM:SS)', required=True)
                title = prompt('Chapter title', required=True)
                url = prompt('Chapter URL (optional)')
                img = prompt('Chapter image URL (optional)')
                toc = prompt_bool('Include in table of contents?', True)
                chapters.append({
                    'startTime': parse_time_to_seconds(start_raw),
                    'title': title,
                    'url': url,
                    'img': img,
                    'toc': toc
                })
                chapters.sort(key=lambda c: c['startTime'])

            if action == 'Edit chapter':
                names = [f'{i+1}. {c["title"]}' for i, c in enumerate(chapters)]
                choice = prompt_choice('Which chapter?', names)
                idx = int(choice.split('.')[0]) - 1
                ch = chapters[idx]
                chapters[idx] = {
                    'startTime': parse_time_to_seconds(prompt('Start time', str(ch['startTime']))),
                    'title': prompt('Title', ch['title'], required=True),
                    'url': prompt('URL', ch.get('url', '')),
                    'img': prompt('Image URL', ch.get('img', '')),
                    'toc': prompt_bool('Include in TOC?', ch.get('toc', True))
                }
                chapters.sort(key=lambda c: c['startTime'])

            if action == 'Delete chapter':
                names = [f'{i+1}. {c["title"]}' for i, c in enumerate(chapters)]
                choice = prompt_choice('Delete which chapter?', names)
                idx = int(choice.split('.')[0]) - 1
                chapters.pop(idx)
        except (CancelAction, KeyboardInterrupt):
            continue

    # Clean up None/empty values
    return [
        {k: v for k, v in ch.items() if v is not None and v != ''}
        for ch in chapters
    ]


# ─── Persons ────────────────────────────────────────────────────────────────

def manage_persons(existing=None):
    persons = list(existing or [])

    while True:
        divider('Persons')
        if not persons:
            print('  No persons yet.\n')
        else:
            for i, p in enumerate(persons, 1):
                print(f'  {i}. {p["name"]} ({p.get("role", "host")})')
            print()

        choices = ['Add person']
        if persons:
            choices.append('Delete person')
        choices.append('Done')

        try:
            action = prompt_choice('Persons', choices)
        except (CancelAction, KeyboardInterrupt):
            return existing if existing is not None else []
        if action == 'Done':
            break

        try:
            if action == 'Add person':
                name = prompt('Name', required=True)
                role = prompt_choice('Role', ['host', 'co-host', 'guest', 'editor', 'producer', 'reporter', 'other'], 'host')
                group = prompt('Group (optional, e.g. "cast")')
                img = prompt('Profile image URL (optional)')
                href = prompt('Profile URL (optional)')
                persons.append({k: v for k, v in {
                    'name': name, 'role': role, 'group': group, 'img': img, 'href': href
                }.items() if v})

            if action == 'Delete person':
                names = [f'{i+1}. {p["name"]}' for i, p in enumerate(persons)]
                choice = prompt_choice('Delete which person?', names)
                idx = int(choice.split('.')[0]) - 1
                persons.pop(idx)
        except (CancelAction, KeyboardInterrupt):
            continue

    return persons


# ─── Description prompt ──────────────────────────────────────────────────────

def prompt_description(existing=None):
    """
    Prompt for episode description / show notes.
    Accepts:
      - A path to a .txt or .html file containing the show notes
      - 'e' to open $EDITOR (nano by default) for multi-line input
      - A single line of plain text typed directly
      - Enter to keep existing value (when editing)
    """
    print('\n  Description / show notes')
    print('    Enter a file path  (e.g. /tmp/shownotes.html or ./notes.txt)')
    print('    Enter "e"          to open in your editor (nano by default)')
    if existing:
        print('    Press Enter        to keep existing description')
    print()

    while True:
        val = read_input('  > ')

        # Keep existing
        if not val and existing is not None:
            return existing
        if not val:
            print('    Required.')
            continue

        # Open in editor
        if val.lower() == 'e':
            import tempfile, subprocess
            editor = os.environ.get('EDITOR', 'nano')
            with tempfile.NamedTemporaryFile(suffix='.txt', mode='w', delete=False, encoding='utf-8') as f:
                if existing:
                    f.write(existing)
                tmpfile = f.name
            try:
                subprocess.call([editor, tmpfile])
                with open(tmpfile, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
            finally:
                os.unlink(tmpfile)
            if content:
                return content
            print('    Empty, try again.')
            continue

        # File path
        if os.path.isfile(val):
            with open(val, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            if content:
                print(f'    Loaded {len(content)} chars from {val}')
                return content
            print('    File is empty, try again.')
            continue

        # Treat as inline text
        return val


# ─── Add episode ─────────────────────────────────────────────────────────────

def add_episode():
    divider('Add Episode')

    title = prompt('Episode title', required=True)
    subtitle = prompt('Subtitle (optional)')
    description = prompt_description()

    while True:
        filename = prompt('Audio filename (must be in ./media/)', required=True)
        fp = os.path.join(MEDIA_DIR, filename)
        if os.path.isfile(fp):
            break
        print(f'    File not found: {fp}')

    while True:
        pub_raw = prompt('Publish date/time (e.g. 2026-06-01 08:00)', required=True)
        try:
            pub_date = parse_pubdate_input(pub_raw)
            break
        except ValueError:
            print('    Invalid date format. Try: 2026-06-01 08:00')

    ep_num = prompt('Episode number (optional)')
    season = prompt('Season number (optional)')
    ep_type = prompt_choice('Episode type', ['full', 'trailer', 'bonus'], 'full')
    artwork = prompt('Episode artwork URL (optional)')
    author = prompt('Episode author (optional)')
    link = prompt('Episode webpage link (optional, shown as "From This Episode" in Apple Podcasts)')
    explicit = prompt_bool('Explicit?', False)
    transcript = prompt('Transcript filename in ./media/ (SRT/VTT/TXT, optional)')
    duration = prompt('Duration (HH:MM:SS or MM:SS, optional)')

    # Location
    location = None
    if prompt_bool('Add podcast:location tag?', False):
        loc_name = prompt('Location name', required=True)
        geo = prompt('Geo URI (e.g. geo:30.2672,97.7431, optional)')
        osm = prompt('OSM identifier (e.g. R113314, optional)')
        location = {k: v for k, v in {'name': loc_name, 'geo': geo, 'osm': osm}.items() if v}

    # Soundbite
    soundbite = None
    if prompt_bool('Add a soundbite?', False):
        sb_start = prompt('Soundbite start time (seconds or HH:MM:SS)', required=True)
        sb_dur = prompt('Soundbite duration in seconds', required=True)
        sb_title = prompt('Soundbite title (optional)')
        soundbite = {k: v for k, v in {
            'startTime': parse_time_to_seconds(sb_start),
            'duration': float(sb_dur),
            'title': sb_title
        }.items() if v is not None}

    chapters = manage_chapters() if prompt_bool('Add chapters?', False) else []
    persons = manage_persons() if prompt_bool('Add podcast:person tags?', False) else []

    ep_id = str(uuid.uuid4())
    episode = {k: v for k, v in {
        'id': ep_id,
        'guid': str(uuid.uuid4()),
        'title': title,
        'subtitle': subtitle,
        'description': description,
        'link': link,
        'filename': filename,
        'pubDate': pub_date,
        'episodeNumber': int(ep_num) if ep_num else None,
        'season': int(season) if season else None,
        'episodeType': ep_type,
        'artwork': artwork,
        'author': author,
        'explicit': explicit,
        'transcript': transcript,
        'duration': duration,
        'filesize': filesize(os.path.join(MEDIA_DIR, filename)),
        'location': location,
        'soundbite': soundbite,
        'chapters': chapters if chapters else None,
        'persons': persons if persons else None
    }.items() if v is not None}

    store.add_episode(episode)
    print(f'\n  Episode added: {title} [{ep_id}]')
    regenerate()


# ─── Edit episode ─────────────────────────────────────────────────────────────

def edit_episode():
    episodes = store.get_episodes()
    if not episodes:
        print('\n  No episodes to edit.\n')
        return

    names = [f'{e["title"]} ({format_date(e["pubDate"])})' for e in episodes]
    while True:
        choice = prompt_choice('Which episode to edit?', names)
        idx = names.index(choice)
        ep = episodes[idx]
        ep_id = ep['id']

        while True:
            try:
                section = prompt_choice('What do you want to edit?', [
                    'Basic info',
                    'Chapters',
                    'Persons',
                    'Soundbite',
                    'Location',
                    'Transcript'
                ])
            except (CancelAction, KeyboardInterrupt):
                break

            try:
                if section == 'Basic info':
                    title = prompt('Title', ep['title'], required=True)
                    subtitle = prompt('Subtitle', ep.get('subtitle', ''))
                    description = prompt_description(ep.get('description'))

                    while True:
                        filename = prompt('Audio filename', ep['filename'], required=True)
                        fp = os.path.join(MEDIA_DIR, filename)
                        if os.path.isfile(fp):
                            break
                        print(f'    File not found: {fp}')

                    while True:
                        pub_raw = prompt('Publish date/time', ep['pubDate'], required=True)
                        try:
                            pub_date = parse_pubdate_input(pub_raw)
                            break
                        except ValueError:
                            print('    Invalid date format.')

                    ep_num = prompt('Episode number', str(ep['episodeNumber']) if ep.get('episodeNumber') else '')
                    season = prompt('Season', str(ep['season']) if ep.get('season') else '')
                    ep_type = prompt_choice('Episode type', ['full', 'trailer', 'bonus'], ep.get('episodeType', 'full'))
                    artwork = prompt('Episode artwork URL', ep.get('artwork', ''))
                    author = prompt('Episode author', ep.get('author', ''))

                    # Episode webpage link. Enter keeps the current value; "-" clears it
                    # (useful for stripping a link inherited from an imported feed).
                    current_link = ep.get('link', '')
                    print(f'    Current link: {current_link or "(none)"}')
                    link_in = prompt('Episode webpage link (Enter=keep, "-" to clear)', current_link or None)
                    link = None if link_in == '-' else link_in

                    explicit = prompt_bool('Explicit?', ep.get('explicit', False))
                    duration = prompt('Duration', ep.get('duration', ''))

                    updates = {k: v for k, v in {
                        'title': title,
                        'subtitle': subtitle or None,
                        'description': description,
                        'link': link,
                        'filename': filename,
                        'pubDate': pub_date,
                        'episodeNumber': int(ep_num) if ep_num else None,
                        'season': int(season) if season else None,
                        'episodeType': ep_type,
                        'artwork': artwork or None,
                        'author': author or None,
                        'explicit': explicit,
                        'duration': duration or None,
                        'filesize': filesize(os.path.join(MEDIA_DIR, filename))
                    }.items() if v is not None or k in ('explicit', 'link')}

                elif section == 'Chapters':
                    existing = ep.get('chapters') or []
                    chapters = manage_chapters(existing)
                    # Managers return the original list only when cancelled.
                    if chapters is existing:
                        continue
                    updates = {'chapters': chapters or None}

                elif section == 'Persons':
                    existing = ep.get('persons') or []
                    persons = manage_persons(existing)
                    if persons is existing:
                        continue
                    updates = {'persons': persons or None}

                elif section == 'Soundbite':
                    if ep.get('soundbite') and prompt_bool('Remove existing soundbite?', False):
                        updates = {'soundbite': None}
                    else:
                        sb = ep.get('soundbite', {})
                        sb_start = prompt('Start time', str(sb.get('startTime', '')), required=True)
                        sb_dur = prompt('Duration (seconds)', str(sb.get('duration', '')), required=True)
                        sb_title = prompt('Title', sb.get('title', ''))
                        updates = {'soundbite': {k: v for k, v in {
                            'startTime': parse_time_to_seconds(sb_start),
                            'duration': float(sb_dur),
                            'title': sb_title or None
                        }.items() if v is not None}}

                elif section == 'Location':
                    if ep.get('location') and prompt_bool('Remove existing location?', False):
                        updates = {'location': None}
                    else:
                        loc = ep.get('location', {})
                        loc_name = prompt('Location name', loc.get('name', ''), required=True)
                        geo = prompt('Geo URI', loc.get('geo', ''))
                        osm = prompt('OSM identifier', loc.get('osm', ''))
                        updates = {'location': {k: v for k, v in {
                            'name': loc_name, 'geo': geo or None, 'osm': osm or None
                        }.items() if v}}

                elif section == 'Transcript':
                    transcript = prompt('Transcript filename (blank to remove)', ep.get('transcript', ''))
                    updates = {'transcript': transcript or None}
            except (CancelAction, KeyboardInterrupt):
                continue

            store.update_episode(ep_id, updates)
            print('\n  Episode updated.')
            regenerate()
            return


# ─── Delete episode ───────────────────────────────────────────────────────────

def delete_episode():
    episodes = store.get_episodes()
    if not episodes:
        print('\n  No episodes to delete.\n')
        return

    names = [f'{e["title"]} ({format_date(e["pubDate"])})' for e in episodes]
    choice = prompt_choice('Which episode to delete?', names)
    idx = names.index(choice)
    ep = episodes[idx]

    if prompt_bool(f'Delete "{ep["title"]}"? (media file is NOT deleted)', False):
        store.delete_episode(ep['id'])
        print('\n  Episode deleted from feed.')
        regenerate()
    else:
        print('\n  Cancelled.\n')


# ─── List episodes ────────────────────────────────────────────────────────────

def list_episodes():
    divider('Episode Library')
    episodes = store.load()['episodes']
    def episode_date(episode):
        try:
            return store.episode_date(episode)
        except (TypeError, ValueError, OverflowError):
            return datetime.min.replace(tzinfo=timezone.utc)
    episodes.sort(key=episode_date, reverse=True)
    if not episodes:
        print('\n  No episodes yet.\n')
        return
    now = datetime.now(timezone.utc)
    rows = []
    for ep in episodes:
        status = 'Published'
        try:
            if store.episode_date(ep) > now:
                status = 'Scheduled'
        except Exception:
            status = 'Invalid date'
        rows.append({'id': ep['id'][:8], 'title': ep['title'],
                     'filename': ep['filename'], 'published': format_date(ep.get('pubDate')),
                     'status': status, 'number': ep.get('episodeNumber'),
                     'season': ep.get('season')})
    episode_table(rows)
    console().print(f'{len(rows)} episodes | Newest first | Dates in UTC', style='muted')


# ─── Import from RSS ──────────────────────────────────────────────────────────

def download_file(url, dest_path, label=''):
    """Download a file with a progress indicator. Returns True on success."""
    return download(url, dest_path, label=label)


def import_from_rss():
    divider('Import from RSS Feed')

    feed_url = prompt('RSS feed URL', required=True)

    try:
        with busy('Fetching RSS feed...'):
            res = requests.get(feed_url, timeout=15, headers={'User-Agent': 'termicast-importer/1.0'})
            res.raise_for_status()
            xml_text = res.text
    except Exception as e:
        print(f'\n  Failed to fetch feed: {e}\n')
        return

    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        print(f'\n  Failed to parse XML: {e}\n')
        return

    ns = {
        'itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd',
        'podcast': 'https://podcastindex.org/namespace/1.0'
    }

    channel = root.find('channel')
    if channel is None:
        print('\n  Could not find RSS channel.\n')
        return

    def get(el, tag, ns_key=None, attr=None, default=''):
        if ns_key:
            found = el.find(f'{ns_key}:{tag}', ns)
        else:
            found = el.find(tag)
        if found is None:
            return default
        if attr:
            return found.get(attr, default)
        return (found.text or default).strip()

    # ── Show metadata ─────────────────────────────────────────────────────────
    itunes_image = channel.find('itunes:image', ns)
    owner = channel.find('itunes:owner', ns)
    show_artwork_url = itunes_image.get('href', '') if itunes_image is not None else ''

    if prompt_bool('Import show-level metadata from this feed?', True):
        show_updates = {k: v for k, v in {
            'title': get(channel, 'title'),
            'description': get(channel, 'description'),
            'author': get(channel, 'author', 'itunes'),
            'email': get(owner, 'email', 'itunes') if owner is not None else '',
            'link': get(channel, 'link'),
            'language': get(channel, 'language') or 'en',
            'explicit': get(channel, 'explicit', 'itunes').strip().lower() in ('true', 'yes'),
        }.items() if v}
        if not store.get_show().get('guid'):
            show_updates['guid'] = str(uuid.uuid4())
        store.save_show(show_updates)
        print('  Show metadata imported.')

        # Download show artwork
        if show_artwork_url:
            ext = os.path.splitext(urlparse(show_artwork_url).path)[-1] or '.jpg'
            art_filename = f'show-artwork{ext}'
            art_dest = os.path.join(MEDIA_DIR, art_filename)
            if not os.path.exists(art_dest):
                print(f'  Downloading show artwork...')
                if download_file(show_artwork_url, art_dest):
                    base_url = store.get_show().get('baseUrl', '').rstrip('/')
                    store.save_show({'artwork': f'{base_url}/media/{art_filename}'})
                    print(f'  Show artwork saved as {art_filename}')
            else:
                print(f'  Show artwork already exists, skipping.')

    # ── Episode selection ─────────────────────────────────────────────────────
    items = channel.findall('item')
    def item_date(item):
        try:
            return store.episode_date({'pubDate': store.parse_rss_date(get(item, 'pubDate'))})
        except (TypeError, ValueError, OverflowError):
            return datetime.min.replace(tzinfo=timezone.utc)
    items.sort(key=item_date, reverse=True)
    print(f'\n  Found {len(items)} episodes.\n')

    if not prompt_bool(f'Import all {len(items)} episodes?', True):
        titles = [get(item, 'title') or f'Episode {i+1}' for i, item in enumerate(items)]
        indices = prompt_checkbox('Select episodes to import (Enter for all)', titles)
        items = [items[i] for i in indices]

    download_audio = prompt_bool('Download audio files?', True)
    download_artwork = prompt_bool('Download episode artwork?', True)
    download_transcripts = prompt_bool('Download transcripts (if available in feed)?', True)

    # ── Import episodes ───────────────────────────────────────────────────────
    imported = 0
    skipped = 0
    existing_guids = {ep.get('guid') for ep in store.get_episodes()}

    for i, item in enumerate(items, 1):
        title = get(item, 'title') or f'Episode {i}'
        progress = Progress(len(items), label=f'  Import: {title}', unit='episodes')
        progress.update(i)
        progress.finish()

        pub_raw = get(item, 'pubDate')
        try:
            pub_date = store.parse_rss_date(pub_raw)
        except (TypeError, ValueError, OverflowError):
            print(color(f'    Invalid or missing pubDate "{pub_raw}", skipping.', '33'))
            skipped += 1
            continue

        enclosure = item.find('enclosure')
        audio_url = enclosure.get('url', '') if enclosure is not None else ''
        length_str = enclosure.get('length', '0') if enclosure is not None else '0'
        guid = get(item, 'guid') or audio_url
        if guid and guid in existing_guids:
            print(color('    Episode already imported, skipping.', '33'))
            skipped += 1
            continue

        # Derive filename from URL, stripping any query params
        try:
            audio_filename = os.path.basename(urlparse(audio_url).path)
        except Exception:
            audio_filename = ''

        if not audio_filename:
            print('    No audio URL found, skipping.')
            skipped += 1
            continue

        # Download audio
        audio_dest = os.path.join(MEDIA_DIR, audio_filename)
        actual_filesize = int(length_str) if length_str.isdigit() else 0

        if download_audio:
            if os.path.exists(audio_dest):
                print(f'    Audio already exists, skipping download.')
                actual_filesize = os.path.getsize(audio_dest)
            else:
                print(f'    Downloading audio: {audio_filename}')
                if download_file(audio_url, audio_dest):
                    actual_filesize = os.path.getsize(audio_dest)
                else:
                    print(color('    Audio download failed, skipping episode; re-import to retry.', '33'))
                    skipped += 1
                    continue

        # Episode artwork
        ep_artwork_url = get(item, 'image', 'itunes', attr='href')
        ep_artwork_local = None
        if download_artwork and ep_artwork_url and ep_artwork_url != show_artwork_url:
            ext = os.path.splitext(urlparse(ep_artwork_url).path)[-1] or '.jpg'
            art_filename = f'{os.path.splitext(audio_filename)[0]}-art{ext}'
            art_dest = os.path.join(MEDIA_DIR, art_filename)
            if os.path.exists(art_dest):
                print(f'    Episode artwork already exists, skipping.')
                ep_artwork_local = f'{store.get_show().get("baseUrl","").rstrip("/")}/media/{art_filename}'
            else:
                print(f'    Downloading episode artwork...')
                if download_file(ep_artwork_url, art_dest):
                    ep_artwork_local = f'{store.get_show().get("baseUrl","").rstrip("/")}/media/{art_filename}'

        # Transcript
        transcript_filename = None
        if download_transcripts:
            transcript_el = item.find('podcast:transcript', ns)
            if transcript_el is not None:
                t_url = transcript_el.get('url', '')
                if t_url:
                    t_ext = os.path.splitext(urlparse(t_url).path)[-1] or '.txt'
                    transcript_filename = f'{os.path.splitext(audio_filename)[0]}{t_ext}'
                    t_dest = os.path.join(MEDIA_DIR, transcript_filename)
                    if os.path.exists(t_dest):
                        print(f'    Transcript already exists, skipping.')
                    else:
                        print(f'    Downloading transcript...')
                        if not download_file(t_url, t_dest):
                            transcript_filename = None

        # Parse dates
        ep_num_str = get(item, 'episode', 'itunes')
        season_str = get(item, 'season', 'itunes')

        # Preserve HTML in description -- ET strips CDATA so we get text content,
        # but content:encoded may have the full HTML version
        def get_html_description(item):
            # Prefer content:encoded which has full HTML
            ce = item.find('{http://purl.org/rss/1.0/modules/content/}encoded')
            if ce is not None and ce.text and ce.text.strip():
                return ce.text.strip()
            # Fall back to description (may be HTML or plain)
            desc = item.find('description')
            if desc is not None and desc.text and desc.text.strip():
                return desc.text.strip()
            return get(item, 'summary', 'itunes') or ''

        description = get_html_description(item)

        # explicit: Substack uses "No"/"Yes", others use "true"/"false" or "clean"
        explicit_raw = get(item, 'explicit', 'itunes').strip().lower()
        is_explicit = explicit_raw in ('true', 'yes')

        # episode link
        ep_link = get(item, 'link') or ''

        episode = {k: v for k, v in {
            'id': str(uuid.uuid4()),
            'guid': guid,
            'title': title,
            'subtitle': get(item, 'subtitle', 'itunes') or None,
            'description': description,
            'link': ep_link or None,
            'filename': audio_filename,
            'pubDate': pub_date,
            'episodeNumber': int(ep_num_str) if ep_num_str.isdigit() else None,
            'season': int(season_str) if season_str.isdigit() else None,
            'episodeType': get(item, 'episodeType', 'itunes') or 'full',
            'explicit': is_explicit,
            'duration': get(item, 'duration', 'itunes') or None,
            'filesize': actual_filesize,
            'artwork': ep_artwork_local or None,
            'transcript': transcript_filename or None,
        }.items() if v is not None}

        store.add_episode(episode)
        existing_guids.add(guid)
        imported += 1
        print(f'    Added to feed.')

    print(color(f'\n  Done. {imported} imported, {skipped} skipped.', '32'))
    regenerate()


# ─── Mirror external feed ─────────────────────────────────────────────────────

def mirror_menu():
    while True:
        try:
            _mirror_menu()
            return
        except (CancelAction, KeyboardInterrupt):
            print(color('\n  Cancelled. Returning to mirror menu.', '33'))


def _mirror_menu():
    import mirror as mirrorgen

    divider('Mirror External Feed')
    data = store.load()
    cfg = data.get('mirror') or {}

    if cfg.get('sourceUrl'):
        print(f'  Source:  {cfg["sourceUrl"]}')
        print(f'  Mirror:  {store.get_show().get("baseUrl", "").rstrip("/")}/mirror/feed.xml')
        print('''
  Every source tag (podcast:guid, value splits, podroll, chapters...) is
  preserved. Episodes are ordered newest-first by publication time, and
  asset URLs and the feed self-link are rewritten locally so the copy
  keeps playing if the source host goes down.''')
        choices = ['Sync now', 'Promote mirror to primary', 'Change source URL', 'Remove mirror', 'Back']
    else:
        print('''
  Mirror an existing feed (e.g. your current host's RSS URL) onto this
  server with original XML tags and newest-first episodes; audio,
  transcripts, chapters, and artwork are downloaded locally so the mirror
  is a self-contained failover if anything happens to your primary host.

  Serve it by symlinking the ./mirror/ folder next to feed.xml, e.g.:
    ln -s /opt/termicast/mirror /var/www/html/mirror

  Re-sync on a schedule with cron:
    0 9 * * * cd /opt/termicast && python3 mirror.py >> /var/log/termicast-mirror.log 2>&1''')
        choices = ['Set up mirror', 'Back']

    try:
        action = prompt_choice('Mirror', choices)
    except CancelAction:
        return

    if action == 'Back':
        return

    if action in ('Set up mirror', 'Change source URL'):
        url = prompt('Source feed URL', cfg.get('sourceUrl'), required=True)
        data = store.load()
        data['mirror'] = {'sourceUrl': url}
        store.save(data)
        print('\n  Mirror configured.')
        if prompt_bool('Run the first sync now? (downloads all audio/artwork)', True):
            action = 'Sync now'
        else:
            return

    if action == 'Sync now':
        print()
        try:
            stats = mirrorgen.sync_mirror(log=print)
            print(color(f'\n  Mirror is live at {stats["feed_url"]}', '32'))
            if stats['assets_failed']:
                print(color(f'  Warning: {stats["assets_failed"]} assets failed to download; '
                            'their URLs still point at the source host. Re-run to retry.', '33'))
        except Exception as e:
            print(color(f'\n  Sync failed: {e}', '31'))
        print()

    elif action == 'Promote mirror to primary':
        import promote as promotegen
        print('''
  This adopts the mirrored feed into YOUR feed, for when the mirrored host
  is gone (or you're leaving it) and this server takes over as the real
  host. It works entirely from the local mirror -- the old host does not
  need to be online.

  What it does:
    - Show metadata, value splits, funding, podroll, categories, and
      locations are imported into your show settings
    - podcast:guid and every episode GUID are preserved EXACTLY, so the
      show keeps its identity and apps don't re-download episodes
    - Audio, transcripts, chapters, and artwork are hardlinked from
      ./mirror/media/ into ./media/ (no extra disk used)
    - Episodes are merged by GUID; nothing already in your database is
      touched
    - podcast:locked becomes "no" so directories will accept the feed URL
      change; feed.xml is regenerated immediately

  Afterwards, update your feed URL in Apple Podcasts Connect, Spotify,
  and podcastindex.org to point at your feed.xml.''')
        if prompt_bool('Promote the mirror into your primary feed?', False):
            print()
            try:
                promotegen.promote_mirror(log=print)
            except Exception as e:
                print(color(f'\n  Promote failed: {e}', '31'))
            print()
        else:
            print('\n  Cancelled.\n')

    elif action == 'Remove mirror':
        if prompt_bool('Remove mirror configuration? (downloaded files are NOT deleted)', False):
            data = store.load()
            data.pop('mirror', None)
            store.save(data)
            print('\n  Mirror removed. Delete ./mirror/ manually if you want the files gone.\n')


# ─── First-run wizard ─────────────────────────────────────────────────────────

def first_run_wizard():
    divider('Welcome to Termicast!')
    print("""
  This looks like your first time running Termicast.
  We need a few basics before you can do anything else.
""")

    # The one thing we really need up front is the base URL --
    # everything else can be filled in later via "Edit show settings"
    print('  Your FQDN is the domain where this podcast will be hosted.')
    print('  Example: https://audio.example.com')
    print('  This is used to build media URLs in your RSS feed.\n')

    while True:
        fqdn = prompt('Your podcast domain (e.g. https://audio.example.com)', required=True)
        # Normalise: ensure https:// prefix, strip trailing slash
        if fqdn and not fqdn.startswith('http'):
            fqdn = 'https://' + fqdn
        fqdn = fqdn.rstrip('/')
        confirm = prompt_bool(f'Use "{fqdn}" as your base URL?', True)
        if confirm:
            break

    print()
    title       = prompt('Show title', required=True)
    description = prompt('Show description', required=True)
    author      = prompt('Your name / author', required=True)
    email       = prompt('Contact email (optional)')
    category    = prompt('Apple Podcasts category (e.g. Technology)', default='Technology')

    print('\n  Use OP3 for download tracking? (https://op3.dev)')
    print('  Recommended -- it\'s free and open source.\n')
    use_op3 = prompt_bool('Enable OP3 tracking?', True)
    op3_prefix = 'https://op3.dev/e/' if use_op3 else ''

    store.save_show({k: v for k, v in {
        'baseUrl':    fqdn,
        'link':       fqdn,
        'title':      title,
        'description': description,
        'author':     author,
        'email':      email or None,
        'category':   category,
        'op3Prefix':  op3_prefix,
        'language':   'en',
        'explicit':   False,
        'guid':       str(uuid.uuid4()),
    }.items() if v is not None})

    print(f"""
  All set! Your podcast is configured at {fqdn}

  Next steps:
    1. Drop your MP3 files into the ./media/ folder
    2. Add episodes with "Add episode"
    3. Point nginx at feed.xml and the media folder
       (see nginx.conf.example)
    4. Set up the daily cron job to publish scheduled episodes
       (see README.md)

  You can update any of these settings later via "Edit show settings".
""")


# ─── Main menu ────────────────────────────────────────────────────────────────

def main():
    banner()
    # First-run wizard fires automatically if podcast.json doesn't exist yet
    if store.is_first_run():
        try:
            first_run_wizard()
        except (CancelAction, KeyboardInterrupt):
            print('\n  Setup cancelled. Run Termicast again when ready.\n')
            return

    while True:
        data = store.load()
        now = datetime.now(timezone.utc)
        scheduled = 0
        for episode in data['episodes']:
            try:
                scheduled += store.episode_date(episode) > now
            except (TypeError, ValueError, OverflowError):
                pass
        dashboard(data['show'].get('title'), len(data['episodes']), scheduled,
                  (data.get('mirror') or {}).get('sourceUrl'), os.path.isfile(feedgen.FEED_FILE))
        try:
            action = main_choice()
        except (CancelAction, KeyboardInterrupt):
            print(color('\n  Already at the main menu. Choose Exit to quit.', '33'))
            continue

        if action == 'Exit':
            print('\n  Bye.\n')
            sys.exit(0)
        try:
            if action == 'Add episode':
                add_episode()
            elif action == 'Edit episode':
                edit_episode()
            elif action == 'Delete episode':
                delete_episode()
            elif action == 'List episodes':
                list_episodes()
            elif action == 'Edit show settings':
                setup_show()
            elif action == 'Import from RSS feed':
                import_from_rss()
            elif action == 'Mirror external feed':
                mirror_menu()
        except (CancelAction, KeyboardInterrupt):
            print(color('\n  Cancelled. Returning to main menu; completed work is kept.', '33'))


def main_choice():
    return prompt_choice('CONTROL ROOM / Choose a number', [
            'Add episode',
            'Edit episode',
            'Delete episode',
            'List episodes',
            'Edit show settings',
            'Import from RSS feed',
            'Mirror external feed',
            'Exit'
        ], groups={
            'Add episode': 'PUBLISH & MANAGE',
            'Edit episode': 'PUBLISH & MANAGE',
            'Delete episode': 'PUBLISH & MANAGE',
            'List episodes': 'PUBLISH & MANAGE',
            'Edit show settings': 'CONFIGURATION',
            'Import from RSS feed': 'MIRROR & MIGRATION',
            'Mirror external feed': 'MIRROR & MIGRATION',
            'Exit': 'SESSION',
        })

if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('\n\n  Bye.\n')
        sys.exit(0)
