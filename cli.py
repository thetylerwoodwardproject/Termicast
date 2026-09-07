#!/usr/bin/env python3
import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
import xml.etree.ElementTree as ET

import store
import feed as feedgen
import pipeline_config
import pipeline as pipelinegen
import transcribe
import llm_client
import audio_tools
import vtt_utils

MEDIA_DIR = os.path.join(os.path.dirname(__file__), 'media')
os.makedirs(MEDIA_DIR, exist_ok=True)


# ─── Prompt helpers ──────────────────────────────────────────────────────────

def prompt(message, default=None, required=False):
    if default:
        msg = f'  {message} [{default}]: '
    else:
        msg = f'  {message}: '
    while True:
        val = input(msg).strip()
        if not val and default is not None:
            return default
        if not val and required:
            print('    Required.')
            continue
        return val or None


def prompt_bool(message, default=True):
    yn = 'Y/n' if default else 'y/N'
    val = input(f'  {message} [{yn}]: ').strip().lower()
    if not val:
        return default
    return val in ('y', 'yes')


def prompt_choice(message, choices, default=None):
    print(f'\n  {message}')
    for i, c in enumerate(choices, 1):
        marker = ' *' if c == default else ''
        print(f'    {i}. {c}{marker}')
    while True:
        val = input('  Choice: ').strip()
        if not val and default:
            return default
        if val.isdigit() and 1 <= int(val) <= len(choices):
            return choices[int(val) - 1]
        print('    Invalid choice.')


def prompt_checkbox(message, choices):
    print(f'\n  {message}')
    for i, c in enumerate(choices, 1):
        print(f'    {i}. {c}')
    print('  Enter numbers separated by spaces (e.g. 1 3 5), or Enter for all:')
    val = input('  > ').strip()
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
        return datetime.fromisoformat(date_str.replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M UTC')
    except Exception:
        return date_str


def filesize(filepath):
    try:
        return os.path.getsize(filepath)
    except Exception:
        return 0


def regenerate():
    live, scheduled = feedgen.write_feed()
    print(f'  feed.xml updated  ({live} live', end='')
    if scheduled:
        print(f', {scheduled} scheduled', end='')
    print(')\n')


def divider(title=''):
    print(f'\n── {title} {"─" * max(0, 50 - len(title))}')


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
    updates['itunesType'] = prompt_choice('Show type', ['episodic', 'serial'], show.get('itunesType', 'episodic'))

    if not show.get('guid'):
        updates['guid'] = str(uuid.uuid4())

    # Remove None values so we don't wipe existing data
    updates = {k: v for k, v in updates.items() if v is not None}
    store.save_show(updates)
    print('\n  Show settings saved.')
    regenerate()


# ─── Advanced Podcast 2.0 settings ────────────────────────────────────────────

def manage_txt_records(existing=None):
    records = list(existing or [])
    while True:
        divider('podcast:txt records')
        if not records:
            print('  None yet.\n')
        else:
            for i, t in enumerate(records, 1):
                purpose = f' (purpose={t["purpose"]})' if t.get('purpose') else ''
                print(f'  {i}. {t["value"]}{purpose}')
            print()
        choices = ['Add record']
        if records:
            choices.append('Delete record')
        choices.append('Done')
        action = prompt_choice('podcast:txt', choices)
        if action == 'Done':
            return records
        if action == 'Add record':
            value = prompt('Value (e.g. a domain verification string)', required=True)
            purpose = prompt('Purpose attribute (optional, e.g. "verify")')
            records.append({k: v for k, v in {'value': value, 'purpose': purpose}.items() if v})
        if action == 'Delete record':
            names = [f'{i+1}. {t["value"]}' for i, t in enumerate(records)]
            choice = prompt_choice('Delete which?', names)
            records.pop(int(choice.split('.')[0]) - 1)


def manage_podroll(existing=None):
    items = list(existing or [])
    print('''
  podcast:podroll recommends other shows to your listeners (Podcast Index
  calls each entry a "remoteItem"). For each show you want to recommend you
  need its Podcast Index feedGuid (podcast:guid from that show's own feed)
  and/or its feedUrl. Look shows up at https://podcastindex.org to find
  their feedGuid. Apple Podcasts and other 2.0 apps show these as "You may
  also like" style recommendations. Up to 8 is a sane practical limit.''')
    while True:
        divider('Podroll')
        if not items:
            print('  No podroll entries yet.\n')
        else:
            for i, it in enumerate(items, 1):
                print(f'  {i}. feedGuid={it.get("feedGuid", "-")}  feedUrl={it.get("feedUrl", "-")}')
            print()
        choices = []
        if len(items) < 8:
            choices.append('Add show')
        if items:
            choices.append('Delete show')
        choices.append('Done')
        action = prompt_choice('Podroll', choices)
        if action == 'Done':
            return items
        if action == 'Add show':
            feed_guid = prompt('feedGuid (from podcastindex.org, optional if you have feedUrl)')
            feed_url = prompt('feedUrl (the other show\'s RSS URL, optional if you have feedGuid)')
            if not feed_guid and not feed_url:
                print('    Need at least one of feedGuid or feedUrl.')
                continue
            items.append({k: v for k, v in {'feedGuid': feed_guid, 'feedUrl': feed_url}.items() if v})
        if action == 'Delete show':
            names = [f'{i+1}. {it.get("feedGuid") or it.get("feedUrl")}' for i, it in enumerate(items)]
            choice = prompt_choice('Delete which?', names)
            items.pop(int(choice.split('.')[0]) - 1)


def advanced_podcast2_settings():
    divider('Advanced Podcast 2.0 Settings')
    show = store.get_show()

    section = prompt_choice('What do you want to configure?', [
        'Lock/Unlock feed',
        'TXT records',
        'Funding URL',
        'Podroll',
        'Creator location',
        'Back'
    ])

    if section == 'Back':
        return

    if section == 'Lock/Unlock feed':
        print('''
  podcast:locked tells other hosting platforms whether they're allowed to
  import this feed and claim ownership. Lock it once you're settled on this
  host; unlock it temporarily if you need to migrate to a different host
  (locking is the default recommendation to prevent feed theft).''')
        locked = prompt_choice('Locked?', ['yes', 'no'], show.get('locked', 'no'))
        owner = prompt('Owner email shown on the lock tag (optional)', show.get('lockedOwner', ''))
        store.save_show({'locked': locked, 'lockedOwner': owner or None})

    elif section == 'TXT records':
        records = manage_txt_records(show.get('txt', []))
        store.save_show({'txt': records})

    elif section == 'Funding URL':
        current = show.get('funding', {})
        url = prompt('Funding URL', current.get('url', ''), required=True)
        text = prompt('Funding button text', current.get('text', 'Support the show'))
        store.save_show({'funding': {'url': url, 'text': text}})

    elif section == 'Podroll':
        items = manage_podroll(show.get('podroll', []))
        store.save_show({'podroll': items})

    elif section == 'Creator location':
        current = show.get('location', {})
        name = prompt('Location name (e.g. "Austin, TX")', current.get('name', ''), required=True)
        geo = prompt('Geo URI (e.g. geo:30.2672,97.7431, optional)', current.get('geo', ''))
        osm = prompt('OSM identifier (e.g. R113314, optional)', current.get('osm', ''))
        store.save_show({'location': {k: v for k, v in {
            'name': name, 'geo': geo or None, 'osm': osm or None
        }.items() if v}})

    print('\n  Saved.')
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

        action = prompt_choice('Chapters', choices)

        if action == 'Done':
            break

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

        action = prompt_choice('Persons', choices)
        if action == 'Done':
            break

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
        val = input('  > ').strip()

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
            subprocess.call([editor, tmpfile])
            with open(tmpfile, 'r', encoding='utf-8') as f:
                content = f.read().strip()
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
    choice = prompt_choice('Which episode to edit?', names)
    idx = names.index(choice)
    ep = episodes[idx]
    ep_id = ep['id']

    section = prompt_choice('What do you want to edit?', [
        'Basic info',
        'Chapters',
        'Persons',
        'Soundbite',
        'Location',
        'Transcript'
    ])

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
        store.update_episode(ep_id, updates)

    elif section == 'Chapters':
        chapters = manage_chapters(ep.get('chapters', []))
        store.update_episode(ep_id, {'chapters': chapters or None})

    elif section == 'Persons':
        persons = manage_persons(ep.get('persons', []))
        store.update_episode(ep_id, {'persons': persons or None})

    elif section == 'Soundbite':
        if ep.get('soundbite') and prompt_bool('Remove existing soundbite?', False):
            store.update_episode(ep_id, {'soundbite': None})
        else:
            sb = ep.get('soundbite', {})
            sb_start = prompt('Start time', str(sb.get('startTime', '')), required=True)
            sb_dur = prompt('Duration (seconds)', str(sb.get('duration', '')), required=True)
            sb_title = prompt('Title', sb.get('title', ''))
            store.update_episode(ep_id, {'soundbite': {k: v for k, v in {
                'startTime': parse_time_to_seconds(sb_start),
                'duration': float(sb_dur),
                'title': sb_title or None
            }.items() if v is not None}})

    elif section == 'Location':
        if ep.get('location') and prompt_bool('Remove existing location?', False):
            store.update_episode(ep_id, {'location': None})
        else:
            loc = ep.get('location', {})
            loc_name = prompt('Location name', loc.get('name', ''), required=True)
            geo = prompt('Geo URI', loc.get('geo', ''))
            osm = prompt('OSM identifier', loc.get('osm', ''))
            store.update_episode(ep_id, {'location': {k: v for k, v in {
                'name': loc_name, 'geo': geo or None, 'osm': osm or None
            }.items() if v}})

    elif section == 'Transcript':
        transcript = prompt('Transcript filename (blank to remove)', ep.get('transcript', ''))
        store.update_episode(ep_id, {'transcript': transcript or None})

    print('\n  Episode updated.')
    regenerate()


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
    episodes = store.get_episodes()
    if not episodes:
        print('\n  No episodes yet.\n')
        return
    print()
    now = datetime.now(timezone.utc)
    for ep in episodes:
        scheduled = ''
        try:
            if datetime.fromisoformat(ep['pubDate'].replace('Z', '+00:00')) > now:
                scheduled = ' [SCHEDULED]'
        except Exception:
            pass
        print(f'  [{ep["id"][:8]}...]{scheduled}')
        print(f'    Title:   {ep["title"]}')
        print(f'    File:    {ep["filename"]}')
        print(f'    PubDate: {format_date(ep["pubDate"])}')
        if ep.get('episodeNumber'):
            s = f' (Season {ep["season"]})' if ep.get('season') else ''
            print(f'    Episode: {ep["episodeNumber"]}{s}')
        print()


# ─── Import from RSS ──────────────────────────────────────────────────────────

def download_file(url, dest_path, label=''):
    """Download a file with a progress indicator. Returns True on success."""
    if not url:
        return False
    # Strip OP3 or any other analytics prefix (anything before https?:// after the first one)
    import re
    url = re.sub(r'^https?://[^/]+/e(?:,\w+)*/(?=https?://)', '', url)
    try:
        res = requests.get(url, stream=True, timeout=30,
                           headers={'User-Agent': 'termicast-importer/1.0'})
        res.raise_for_status()
        total = int(res.headers.get('content-length', 0))
        downloaded = 0
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, 'wb') as f:
            for chunk in res.iter_content(chunk_size=65536):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = int(downloaded / total * 40)
                    bar = '█' * pct + '░' * (40 - pct)
                    mb = downloaded / 1_000_000
                    print(f'\r    [{bar}] {mb:.1f} MB', end='', flush=True)
        print()
        return True
    except Exception as e:
        print(f'\n    Failed: {e}')
        # Remove partial file
        if os.path.exists(dest_path):
            os.remove(dest_path)
        return False


def import_from_rss():
    divider('Import from RSS Feed')

    feed_url = prompt('RSS feed URL', required=True)

    print('  Fetching feed...')
    try:
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

    for i, item in enumerate(items, 1):
        title = get(item, 'title') or f'Episode {i}'
        print(f'\n  [{i}/{len(items)}] {title}')

        enclosure = item.find('enclosure')
        audio_url = enclosure.get('url', '') if enclosure is not None else ''
        length_str = enclosure.get('length', '0') if enclosure is not None else '0'

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
                    print('    Audio download failed, episode will still be added to database.')

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
        guid_el = item.find('guid')
        guid = (guid_el.text or '').strip() if guid_el is not None else str(uuid.uuid4())

        pub_raw = get(item, 'pubDate')
        pub_date = datetime.now(timezone.utc).isoformat()
        for fmt in ('%a, %d %b %Y %H:%M:%S %z', '%a, %d %b %Y %H:%M:%S GMT'):
            try:
                pub_date = datetime.strptime(pub_raw, fmt).isoformat()
                break
            except Exception:
                continue

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
        imported += 1
        print(f'    Added to feed.')

    print(f'\n  Done. {imported} imported, {skipped} skipped.')
    regenerate()


# ─── Mirror external feed ─────────────────────────────────────────────────────

def mirror_menu():
    import mirror as mirrorgen

    divider('Mirror External Feed')
    data = store.load()
    cfg = data.get('mirror') or {}

    if cfg.get('sourceUrl'):
        print(f'  Source:  {cfg["sourceUrl"]}')
        print(f'  Mirror:  {store.get_show().get("baseUrl", "").rstrip("/")}/mirror/feed.xml')
        print('''
  The mirror is a verbatim copy of the source feed: every tag (podcast:guid,
  value splits, podroll, chapters...) is preserved exactly. Only asset URLs
  are rewritten to local downloads so the copy keeps playing if the source
  host goes down.''')
        choices = ['Sync now', 'Promote mirror to primary', 'Change source URL', 'Remove mirror', 'Back']
    else:
        print('''
  Mirror an existing feed (e.g. your current host's RSS URL) onto this
  server as an exact copy. The source XML is kept byte-for-byte; audio,
  transcripts, chapters, and artwork are downloaded locally so the mirror
  is a self-contained failover if anything happens to your primary host.

  Serve it by symlinking the ./mirror/ folder next to feed.xml, e.g.:
    ln -s /opt/termicast/mirror /var/www/html/mirror

  Re-sync on a schedule with cron:
    0 9 * * * cd /opt/termicast && python3 mirror.py >> /var/log/termicast-mirror.log 2>&1''')
        choices = ['Set up mirror', 'Back']

    action = prompt_choice('Mirror', choices)

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
            print(f'\n  Mirror is live at {stats["feed_url"]}')
            if stats['assets_failed']:
                print(f'  Warning: {stats["assets_failed"]} assets failed to download; '
                      'their URLs still point at the source host. Re-run to retry.')
        except Exception as e:
            print(f'\n  Sync failed: {e}')
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
                print(f'\n  Promote failed: {e}')
            print()
        else:
            print('\n  Cancelled.\n')

    elif action == 'Remove mirror':
        if prompt_bool('Remove mirror configuration? (downloaded files are NOT deleted)', False):
            data = store.load()
            data.pop('mirror', None)
            store.save(data)
            print('\n  Mirror removed. Delete ./mirror/ manually if you want the files gone.\n')


# ─── AI pipeline: config wizard ───────────────────────────────────────────────

def configure_pipeline():
    divider('Configure AI Pipeline')
    config = pipeline_config.load()

    section = prompt_choice('What do you want to configure?', [
        'Transcription (Whisper)',
        'LLM provider (Claude/OpenAI)',
        'Prompts / tone',
        'Soundbite rules',
        'Waveform video',
        'Recurring publish schedule',
        'Back'
    ])
    if section == 'Back':
        return

    if section == 'Transcription (Whisper)':
        mode = prompt_choice('Mode', ['cloud', 'local'], config['transcription']['mode'])
        config['transcription']['mode'] = mode
        if mode == 'cloud':
            config['transcription']['cloud']['apiKeyEnv'] = prompt(
                'Env var holding the OpenAI API key', config['transcription']['cloud']['apiKeyEnv'])
            config['transcription']['cloud']['model'] = prompt(
                'Whisper model', config['transcription']['cloud']['model'])
        else:
            config['transcription']['local']['command'] = prompt(
                'Local command template (must use {input} and {outdir}, and write "<basename>.vtt")',
                config['transcription']['local']['command'])
        print('\n  API keys are read from environment variables at run time -- never stored in pipeline.json.')

    elif section == 'LLM provider (Claude/OpenAI)':
        provider = prompt_choice('Provider', ['claude', 'openai'], config['llm']['provider'])
        config['llm']['provider'] = provider
        if provider == 'claude':
            config['llm']['claude']['apiKeyEnv'] = prompt(
                'Env var holding the Anthropic API key', config['llm']['claude']['apiKeyEnv'])
            config['llm']['claude']['model'] = prompt('Claude model', config['llm']['claude']['model'])
        else:
            config['llm']['openai']['apiKeyEnv'] = prompt(
                'Env var holding the OpenAI API key', config['llm']['openai']['apiKeyEnv'])
            config['llm']['openai']['model'] = prompt('OpenAI model', config['llm']['openai']['model'])

    elif section == 'Prompts / tone':
        print('  These instructions are sent to the LLM for each generation step.')
        print('  Enter new text for each, or press Enter to keep the current instructions.\n')
        for key in ('tone', 'titles', 'description', 'keywords', 'chapters', 'soundbites', 'socialPosts'):
            config['prompts'][key] = prompt(key, config['prompts'][key])

    elif section == 'Soundbite rules':
        sb = config['soundbites']
        sb['count'] = int(prompt('Number of soundbites to generate', str(sb['count'])))
        sb['minDurationSeconds'] = float(prompt('Minimum clip length (seconds)', str(sb['minDurationSeconds'])))
        sb['maxDurationSeconds'] = float(prompt('Maximum clip length (seconds)', str(sb['maxDurationSeconds'])))
        sb['maxTotalDurationSeconds'] = float(prompt(
            'Maximum combined length of all soundbites (seconds)', str(sb['maxTotalDurationSeconds'])))
        sb['titleMaxChars'] = int(prompt('Max soundbite title length (chars)', str(sb['titleMaxChars'])))

    elif section == 'Waveform video':
        v = config['video']
        v['enabled'] = prompt_bool('Generate waveform MP4s for soundbites?', v['enabled'])
        if v['enabled']:
            v['bgColor'] = prompt_choice(
                'Background color (solid, for chroma-key compositing in Canva)',
                ['RED', 'GREEN', 'BLUE'], v['bgColor'])
            v['waveformColorHex'] = prompt('Waveform line color (hex, e.g. #FF4500)', v['waveformColorHex'])
            v['fps'] = int(prompt('Frames per second', str(v['fps'])))

    elif section == 'Recurring publish schedule':
        r = config['schedule']['recurring']
        r['enabled'] = prompt_bool('Auto-schedule processed episodes on a recurring weekly slot?', r['enabled'])
        if r['enabled']:
            r['dayOfWeek'] = prompt_choice('Day of week', pipeline_config.WEEKDAYS, r['dayOfWeek'])
            r['time'] = prompt('Time (HH:MM, 24h, UTC)', r['time'])
            print('\n  Reminder: cron must run generate.py at least as often as your slot')
            print('  granularity (e.g. hourly) for episodes to go live promptly. See README.')

    pipeline_config.save(config)
    print('\n  Pipeline config saved to pipeline.json.')


# ─── AI pipeline: process a new episode ───────────────────────────────────────

AUDIO_INPUT_EXTS = ('.wav', '.flac', '.mp3')


def _pick_input_files(input_dir):
    """Find the audio file and (optional) PNG artwork in an episode's input folder."""
    entries = os.listdir(input_dir)
    audio_files = sorted(f for f in entries if f.lower().endswith(AUDIO_INPUT_EXTS))
    art_files = sorted(f for f in entries if f.lower().endswith('.png'))

    if not audio_files:
        print(f'    No WAV/FLAC/MP3 file found in {input_dir}')
        return None, None
    if len(audio_files) > 1:
        audio_file = prompt_choice('Multiple audio files found, which one?', audio_files)
    else:
        audio_file = audio_files[0]

    art_file = None
    if len(art_files) > 1:
        art_file = prompt_choice('Multiple PNG files found, which is the artwork?', art_files)
    elif art_files:
        art_file = art_files[0]
    else:
        print('    No PNG artwork found -- continuing without episode artwork.')

    return audio_file, art_file


def process_new_episode_ai():
    divider('Process New Episode (AI Pipeline)')
    config = pipeline_config.load()

    input_dir = prompt('Folder containing the episode audio (WAV/FLAC/MP3) and artwork PNG', required=True)
    if not os.path.isdir(input_dir):
        print(f'\n  Not a folder: {input_dir}\n')
        return

    audio_file, art_file = _pick_input_files(input_dir)
    if not audio_file:
        return

    ep_id = str(uuid.uuid4())
    work_dir = os.path.join(config['workDir'], ep_id)
    os.makedirs(work_dir, exist_ok=True)

    print('\n  [1/7] Converting audio to MP3...')
    converted_mp3 = os.path.join(work_dir, 'episode.mp3')
    try:
        audio_tools.convert_audio(os.path.join(input_dir, audio_file), converted_mp3)
    except audio_tools.AudioToolsError as e:
        print(f'\n  Audio conversion failed: {e}\n')
        return

    print('  [2/7] Transcribing (this can take a while for a full episode)...')
    vtt_path = os.path.join(work_dir, 'transcript.vtt')
    try:
        transcribe.transcribe_to_vtt(converted_mp3, config, vtt_path)
    except transcribe.TranscriptionError as e:
        print(f'\n  Transcription failed: {e}\n')
        return

    cues = vtt_utils.parse_vtt(vtt_path)
    if not cues:
        print('\n  Transcript came back empty, aborting.\n')
        return
    transcript_text = vtt_utils.full_text(cues)
    transcript_ts = vtt_utils.transcript_with_timestamps(cues)
    total_duration = vtt_utils.duration(cues) or audio_tools.ffprobe_duration(converted_mp3)

    print('  [3/7] Generating titles, description, and keywords...')
    try:
        titles = llm_client.generate_titles(transcript_text, config)
        chosen_title = prompt_choice('Pick the best title', titles)
        chosen_idx = titles.index(chosen_title)

        description = llm_client.generate_description(transcript_text, chosen_title, config)
        description = prompt_description(description)

        keywords = llm_client.generate_keywords(transcript_text, config)
    except llm_client.LLMError as e:
        print(f'\n  LLM generation failed: {e}\n')
        return

    print('  [4/7] Generating chapters...')
    try:
        chapters = llm_client.generate_chapters(transcript_ts, config)
    except llm_client.LLMError as e:
        print(f'    Chapter generation failed, continuing without chapters: {e}')
        chapters = []
    for ch in chapters:
        if ch.get('endTime') and ch['endTime'] > total_duration:
            ch['endTime'] = total_duration

    print('  [5/7] Selecting and rendering soundbites...')
    sb_cfg = config['soundbites']
    try:
        candidates = llm_client.generate_soundbites(
            transcript_ts, sb_cfg['count'], sb_cfg['minDurationSeconds'], sb_cfg['maxDurationSeconds'], config)
    except llm_client.LLMError as e:
        print(f'    Soundbite selection failed, continuing without soundbites: {e}')
        candidates = []
    soundbites = pipelinegen.finalize_soundbites(candidates, total_duration, sb_cfg)

    # Determine publish date before finalizing filenames (they're date-prefixed)
    sched = config['schedule']['recurring']
    pub_date = None
    if sched.get('enabled'):
        existing_pubdates = [e['pubDate'] for e in store.get_episodes()]
        slot = pipelinegen.next_recurring_slot(config, existing_pubdates)
        print(f'\n  Next recurring slot: {format_date(slot)}')
        if prompt_bool('Use this date?', True):
            pub_date = slot
    if pub_date is None:
        while True:
            pub_raw = prompt('Publish date/time (e.g. 2026-06-01 08:00)', required=True)
            try:
                pub_date = parse_pubdate_input(pub_raw)
                break
            except ValueError:
                print('    Invalid date format. Try: 2026-06-01 08:00')

    prefix = pipelinegen.date_prefix(pub_date)
    slug = pipelinegen.slugify(chosen_title, max_len=60)

    final_audio_filename = f'{prefix}_{slug}.mp3'
    shutil.copyfile(converted_mp3, os.path.join(MEDIA_DIR, final_audio_filename))

    artwork_url = None
    if art_file:
        ext = os.path.splitext(art_file)[1] or '.png'
        artwork_filename = f'{prefix}_{slug}-art{ext}'
        shutil.copyfile(os.path.join(input_dir, art_file), os.path.join(MEDIA_DIR, artwork_filename))
        base_url = store.get_show().get('baseUrl', '').rstrip('/')
        artwork_url = f'{base_url}/media/{artwork_filename}'

    transcript_filename = f'{prefix}_{slug}.vtt'
    shutil.copyfile(vtt_path, os.path.join(MEDIA_DIR, transcript_filename))

    final_audio_path = os.path.join(MEDIA_DIR, final_audio_filename)
    for sb in soundbites:
        sb_slug = pipelinegen.slugify(sb['title'], max_len=60)
        mp3_filename = f'{prefix}_{sb_slug}.mp3'
        mp4_filename = f'{prefix}_{sb_slug}.mp4'
        print(f'    Extracting soundbite: {sb["title"]}')
        audio_tools.extract_clip(final_audio_path, os.path.join(MEDIA_DIR, mp3_filename),
                                  sb['startTime'], sb['endTime'])
        sb['mp3'] = mp3_filename
        sb['caption'] = vtt_utils.text_between(cues, sb['startTime'], sb['endTime'])

        sb['mp4'] = None
        if config['video'].get('enabled', True):
            print(f'    Rendering waveform video for: {sb["title"]}')
            try:
                audio_tools.render_waveform_video(
                    os.path.join(MEDIA_DIR, mp3_filename), os.path.join(MEDIA_DIR, mp4_filename), config['video'])
                sb['mp4'] = mp4_filename
            except audio_tools.AudioToolsError as e:
                print(f'      Video render failed, keeping the MP3 only: {e}')

    print('  [6/7] Generating social media posts...')
    try:
        social_posts = llm_client.generate_social_posts(
            transcript_text, chosen_title, feedgen.strip_html(description), config)
    except llm_client.LLMError as e:
        print(f'    Social post generation failed, continuing without them: {e}')
        social_posts = []

    print('  [7/7] Saving episode and writing editorial file...')
    episode = {
        'id': ep_id,
        'guid': str(uuid.uuid4()),
        'title': chosen_title,
        'description': description,
        'filename': final_audio_filename,
        'pubDate': pub_date,
        'episodeType': 'full',
        'explicit': False,
        'transcript': transcript_filename,
        'duration': str(int(total_duration)),
        'filesize': filesize(final_audio_path),
        'artwork': artwork_url,
        'chapters': chapters,
        'keywords': keywords,
        'titleOptions': titles,
        'titleChosenIndex': chosen_idx,
        'soundbites': soundbites,
        'socialPosts': social_posts,
        'pipelineMeta': {
            'sourceAudio': audio_file,
            'generatedAt': datetime.now(timezone.utc).isoformat(),
        },
    }
    store.add_episode(episode)
    md_path = pipelinegen.write_editorial_markdown(episode, config)
    store.update_episode(ep_id, {'pipelineMeta': {**episode['pipelineMeta'], 'mdFile': md_path}})

    print(f'\n  Episode processed: {chosen_title}')
    print(f'  Scheduled for:     {format_date(pub_date)}')
    print(f'  Soundbites:        {len(soundbites)}')
    print(f'  Editorial file:    {md_path}')
    regenerate()


# ─── AI pipeline: edit a processed episode ────────────────────────────────────

def edit_processed_episode_ai():
    episodes = [e for e in store.get_episodes() if e.get('pipelineMeta')]
    if not episodes:
        print('\n  No AI-pipeline-processed episodes yet.\n')
        return

    names = [f'{e["title"]} ({format_date(e["pubDate"])})' for e in episodes]
    choice = prompt_choice('Which processed episode?', names)
    ep = episodes[names.index(choice)]
    ep_id = ep['id']
    config = pipeline_config.load()

    transcript_path = os.path.join(MEDIA_DIR, ep['transcript']) if ep.get('transcript') else None
    cues = vtt_utils.parse_vtt(transcript_path) if transcript_path and os.path.isfile(transcript_path) else []
    transcript_text = vtt_utils.full_text(cues)
    transcript_ts = vtt_utils.transcript_with_timestamps(cues)

    section = prompt_choice('What do you want to redo?', [
        'Title (pick a different generated option)',
        'Description (regenerate via AI)',
        'Keywords (regenerate via AI)',
        'Chapters (regenerate via AI)',
        'Soundbites (regenerate via AI)',
        'Social posts (regenerate via AI)',
        'Regenerate editorial file only',
        'Back'
    ])
    if section == 'Back':
        return

    if section.startswith('Title'):
        options = ep.get('titleOptions') or [ep['title']]
        chosen = prompt_choice('Pick a title', options)
        store.update_episode(ep_id, {'title': chosen, 'titleChosenIndex': options.index(chosen)})

    elif section.startswith('Description'):
        if not transcript_text:
            print('\n  No transcript on file for this episode, cannot regenerate.\n')
            return
        try:
            description = llm_client.generate_description(transcript_text, ep['title'], config)
        except llm_client.LLMError as e:
            print(f'\n  Failed: {e}\n')
            return
        description = prompt_description(description)
        store.update_episode(ep_id, {'description': description})

    elif section.startswith('Keywords'):
        if not transcript_text:
            print('\n  No transcript on file for this episode, cannot regenerate.\n')
            return
        try:
            keywords = llm_client.generate_keywords(transcript_text, config)
        except llm_client.LLMError as e:
            print(f'\n  Failed: {e}\n')
            return
        store.update_episode(ep_id, {'keywords': keywords})

    elif section.startswith('Chapters'):
        if not transcript_ts:
            print('\n  No transcript on file for this episode, cannot regenerate.\n')
            return
        try:
            chapters = llm_client.generate_chapters(transcript_ts, config)
        except llm_client.LLMError as e:
            print(f'\n  Failed: {e}\n')
            return
        store.update_episode(ep_id, {'chapters': chapters})

    elif section.startswith('Soundbites'):
        if not transcript_ts or not cues:
            print('\n  No transcript on file for this episode, cannot regenerate.\n')
            return
        total_duration = vtt_utils.duration(cues)
        sb_cfg = config['soundbites']
        try:
            candidates = llm_client.generate_soundbites(
                transcript_ts, sb_cfg['count'], sb_cfg['minDurationSeconds'], sb_cfg['maxDurationSeconds'], config)
        except llm_client.LLMError as e:
            print(f'\n  Failed: {e}\n')
            return
        soundbites = pipelinegen.finalize_soundbites(candidates, total_duration, sb_cfg)

        prefix = pipelinegen.date_prefix(ep['pubDate'])
        final_audio_path = os.path.join(MEDIA_DIR, ep['filename'])
        for sb in soundbites:
            sb_slug = pipelinegen.slugify(sb['title'], max_len=60)
            mp3_filename = f'{prefix}_{sb_slug}.mp3'
            mp4_filename = f'{prefix}_{sb_slug}.mp4'
            print(f'    Extracting soundbite: {sb["title"]}')
            audio_tools.extract_clip(final_audio_path, os.path.join(MEDIA_DIR, mp3_filename),
                                      sb['startTime'], sb['endTime'])
            sb['mp3'] = mp3_filename
            sb['caption'] = vtt_utils.text_between(cues, sb['startTime'], sb['endTime'])
            sb['mp4'] = None
            if config['video'].get('enabled', True):
                try:
                    audio_tools.render_waveform_video(
                        os.path.join(MEDIA_DIR, mp3_filename), os.path.join(MEDIA_DIR, mp4_filename), config['video'])
                    sb['mp4'] = mp4_filename
                except audio_tools.AudioToolsError as e:
                    print(f'      Video render failed, keeping the MP3 only: {e}')
        print('\n  Note: previously generated soundbite MP3/MP4 files are NOT deleted; clean up ./media/ manually if needed.')
        store.update_episode(ep_id, {'soundbites': soundbites})

    elif section.startswith('Social posts'):
        if not transcript_text:
            print('\n  No transcript on file for this episode, cannot regenerate.\n')
            return
        try:
            social_posts = llm_client.generate_social_posts(
                transcript_text, ep['title'], feedgen.strip_html(ep.get('description', '')), config)
        except llm_client.LLMError as e:
            print(f'\n  Failed: {e}\n')
            return
        store.update_episode(ep_id, {'socialPosts': social_posts})

    updated = store.get_episode(ep_id)
    md_path = pipelinegen.write_editorial_markdown(updated, config)
    store.update_episode(ep_id, {'pipelineMeta': {**updated.get('pipelineMeta', {}), 'mdFile': md_path}})
    print(f'\n  Updated. Editorial file refreshed: {md_path}')
    regenerate()


# ─── First-run wizard ─────────────────────────────────────────────────────────

def first_run_wizard():
    print('\n╔══════════════════════════════════════╗')
    print('║     Welcome to Termicast!            ║')
    print('║     Let\'s get you set up.            ║')
    print('╚══════════════════════════════════════╝')
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
    # First-run wizard fires automatically if podcast.json doesn't exist yet
    if store.is_first_run():
        first_run_wizard()

    print('\n╔══════════════════════════════════════╗')
    print('║   Termicast - Podcast Feed Manager   ║')
    print('╚══════════════════════════════════════╝')

    while True:
        action = prompt_choice('\nWhat do you want to do?', [
            'Add episode',
            'Edit episode',
            'Delete episode',
            'List episodes',
            'Process new episode (AI pipeline)',
            'Edit processed episode (AI pipeline)',
            'Configure AI pipeline',
            'Edit show settings',
            'Advanced Podcast 2.0 settings',
            'Import from RSS feed',
            'Mirror external feed',
            'Exit'
        ])

        if action == 'Exit':
            print('\n  Bye.\n')
            sys.exit(0)
        elif action == 'Add episode':
            add_episode()
        elif action == 'Edit episode':
            edit_episode()
        elif action == 'Delete episode':
            delete_episode()
        elif action == 'List episodes':
            list_episodes()
        elif action == 'Process new episode (AI pipeline)':
            process_new_episode_ai()
        elif action == 'Edit processed episode (AI pipeline)':
            edit_processed_episode_ai()
        elif action == 'Configure AI pipeline':
            configure_pipeline()
        elif action == 'Edit show settings':
            setup_show()
        elif action == 'Advanced Podcast 2.0 settings':
            advanced_podcast2_settings()
        elif action == 'Import from RSS feed':
            import_from_rss()
        elif action == 'Mirror external feed':
            mirror_menu()


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('\n\n  Bye.\n')
        sys.exit(0)
