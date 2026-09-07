"""
promote.py - adopt a mirrored feed as this server's primary feed

For when the mirrored host is gone (or you're leaving it) and Termicast
takes over as the real host. Reads the LOCAL mirror (./mirror/feed.xml and
its downloaded assets -- the source host does not need to be reachable) and
imports it into podcast.json so new episodes can be published here.

Unlike the old "Import from RSS feed" migration, promotion preserves the
show's identity and Podcasting 2.0 surface:

  - podcast:guid is kept exactly (this is the show's identity in the
    Podcast Index ecosystem -- minting a new one would sever the show)
  - episode GUIDs are kept exactly, so apps don't re-download everything
  - value/valueRecipient splits, funding, license, medium, podroll,
    locations, copyright, and ALL categories are carried into the database
    and re-emitted by feed.py
  - podcast:locked is set to "no" so directories will accept the feed URL
    change away from the dead host

Media, transcripts, chapter JSON, and artwork are hardlinked (copied if
hardlinking fails) from ./mirror/media/ into ./media/, so promotion costs
no extra disk and the mirror stays intact as a fallback.

Episodes are merged by GUID: existing episodes are left alone, only
missing ones are added.
"""
import json
import os
import re
import shutil
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import store
import mirror as mirrormod

BASE_DIR = os.environ.get('TERMICAST_DATA_DIR') or os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR = os.path.join(BASE_DIR, 'media')

NS = {
    'podcast': 'https://podcastindex.org/namespace/1.0',
    'itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd',
    'psc': 'http://podlove.org/simple-chapters',
    'content': 'http://purl.org/rss/1.0/modules/content/',
}


def _text(el, path):
    found = el.find(path, NS) if el is not None else None
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def _attr(el, path, attr):
    found = el.find(path, NS) if el is not None else None
    return found.get(attr) if found is not None else None


def _seconds(s):
    """Parse '8:27', '1:02:03', or plain seconds into a number."""
    s = str(s).strip()
    try:
        parts = [float(p) for p in s.split(':')]
    except ValueError:
        return 0
    total = 0
    for p in parts:
        total = total * 60 + p
    return int(total) if total == int(total) else total


def _localize(url, log):
    """Map a mirror asset URL to a file in ./media/, hardlinking it over
    from ./mirror/media/. Returns the local filename, or None if the asset
    isn't in the mirror (e.g. its download failed)."""
    name = os.path.basename(urlparse(url).path)
    src = os.path.join(mirrormod.MIRROR_MEDIA_DIR, name)
    if not name or not os.path.exists(src):
        return None
    dest = os.path.join(MEDIA_DIR, name)
    if not os.path.exists(dest):
        os.makedirs(MEDIA_DIR, exist_ok=True)
        try:
            os.link(src, dest)
        except OSError:
            shutil.copy2(src, dest)
    return name


def _parse_location(el):
    if el is None:
        return None
    loc = {'name': (el.text or '').strip()}
    for key in ('rel', 'geo', 'osm', 'country'):
        if el.get(key):
            loc[key] = el.get(key)
    return loc


def _parse_chapters(ep_local_json, media_base_url, log):
    """Turn an adopted chapters JSON file into the editable chapters list
    used by the database, localizing chapter images along the way."""
    path = os.path.join(MEDIA_DIR, ep_local_json)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        log(f'    could not parse chapters JSON {ep_local_json}: {e}')
        return None
    chapters = []
    for ch in data.get('chapters', []):
        entry = {'startTime': ch.get('startTime', 0), 'title': ch.get('title', '')}
        if ch.get('url'):
            entry['url'] = ch['url']
        if ch.get('img'):
            name = _localize(ch['img'], log)
            entry['img'] = f'{media_base_url}/{name}' if name else ch['img']
        if ch.get('toc') is False:
            entry['toc'] = False
        chapters.append(entry)
    return chapters or None


def _psc_to_chapters(item, media_base_url, log):
    """Fallback: convert inline Podlove psc:chapters when there is no
    podcast:chapters JSON."""
    chapters = []
    for ch in item.findall('psc:chapters/psc:chapter', NS):
        entry = {'startTime': _seconds(ch.get('start', '0')), 'title': ch.get('title', '')}
        if ch.get('href'):
            entry['url'] = ch.get('href')
        if ch.get('image'):
            name = _localize(ch.get('image'), log)
            entry['img'] = f'{media_base_url}/{name}' if name else ch.get('image')
        chapters.append(entry)
    return chapters or None


def promote_mirror(log=print):
    """Adopt the local mirror into podcast.json. Returns a stats dict."""
    if not os.path.exists(mirrormod.MIRROR_FEED_FILE):
        raise RuntimeError('No mirror found. Sync the mirror at least once first.')

    data = store.load()
    base_url = data['show'].get('baseUrl', '').rstrip('/')
    if not base_url:
        raise RuntimeError('Show baseUrl is not set; run the setup wizard first.')
    media_base_url = f'{base_url}/media'

    tree = ET.parse(mirrormod.MIRROR_FEED_FILE)
    channel = tree.getroot().find('channel')
    if channel is None:
        raise RuntimeError('Mirror feed has no <channel>.')

    # ── Show metadata ─────────────────────────────────────────────────────
    show = {}
    for field, path in (
        ('title', 'title'), ('description', 'description'), ('link', 'link'),
        ('language', 'language'), ('copyright', 'copyright'),
        ('author', 'itunes:author'), ('email', 'itunes:owner/itunes:email'),
        ('license', 'podcast:license'), ('medium', 'podcast:medium'),
    ):
        val = _text(channel, path)
        if val:
            show[field] = val

    guid = _text(channel, 'podcast:guid')
    if guid:
        show['guid'] = guid  # preserved exactly: this is the show's identity

    show['explicit'] = (_text(channel, 'itunes:explicit') or '').lower() in ('true', 'yes')

    # Unlock so directories accept the feed URL change away from the old host
    show['locked'] = 'no'
    locked_el = channel.find('podcast:locked', NS)
    if locked_el is not None and locked_el.get('owner'):
        show['lockedOwner'] = locked_el.get('owner')

    art_url = _attr(channel, 'itunes:image', 'href') or _text(channel, 'image/url')
    if art_url:
        name = _localize(art_url, log)
        show['artwork'] = f'{media_base_url}/{name}' if name else art_url

    cats = []
    for cat in channel.findall('itunes:category', NS):
        sub = cat.find('itunes:category', NS)
        cats.append({'category': cat.get('text', ''),
                     'subcategory': sub.get('text') if sub is not None else None})
    if cats:
        show['categories'] = cats
        show['category'] = cats[0]['category']
        show['subcategory'] = cats[0].get('subcategory')

    funding = channel.find('podcast:funding', NS)
    if funding is not None:
        show['funding'] = {'url': funding.get('url', ''), 'text': (funding.text or '').strip()}

    loc = _parse_location(channel.find('podcast:location', NS))
    if loc:
        show['location'] = loc

    value = channel.find('podcast:value', NS)
    if value is not None:
        recipients = []
        for r in value.findall('podcast:valueRecipient', NS):
            recipients.append({k: r.get(k) for k in
                               ('name', 'type', 'address', 'split', 'customKey', 'customValue', 'fee')
                               if r.get(k) is not None})
        show['value'] = {'type': value.get('type', ''), 'method': value.get('method', ''),
                         'recipients': recipients}
        if value.get('suggested'):
            show['value']['suggested'] = value.get('suggested')

    podroll = channel.find('podcast:podroll', NS)
    if podroll is not None:
        show['podroll'] = [
            {k: ri.get(k) for k in ('feedGuid', 'feedUrl', 'itemGuid') if ri.get(k)}
            for ri in podroll.findall('podcast:remoteItem', NS)
        ]

    # ── Episodes (merged by GUID) ─────────────────────────────────────────
    existing_guids = {e.get('guid') for e in data['episodes']}
    added = skipped_dup = skipped_missing = 0

    from display import color, Progress
    items = channel.findall('item')
    for index, item in enumerate(items, 1):
        title = _text(item, 'title') or 'Untitled'
        progress = Progress(len(items), label=f'  Promote: {title}', log=log, unit='episodes')
        progress.update(index)
        progress.finish()
        guid_el = item.find('guid')
        ep_guid = (guid_el.text or '').strip() if guid_el is not None else None
        if not ep_guid:
            log(f'    "{title}": no GUID, skipping')
            skipped_missing += 1
            continue
        if ep_guid in existing_guids:
            skipped_dup += 1
            continue

        enclosure_url = _attr(item, 'enclosure', 'url') or ''
        filename = _localize(enclosure_url, log)
        if not filename:
            log(f'    "{title}": audio not in mirror ({enclosure_url}), skipping')
            skipped_missing += 1
            continue

        pub_raw = _text(item, 'pubDate')
        try:
            pub_date = store.parse_rss_date(pub_raw)
        except Exception:
            log(f'    "{title}": unparseable pubDate "{pub_raw}", skipping')
            skipped_missing += 1
            continue

        episode = {
            # id is used in generated filenames (chapters JSON), so keep it
            # filesystem-safe; guid stays exact
            'id': re.sub(r'[^A-Za-z0-9._-]', '_', ep_guid),
            'guid': ep_guid,
            'title': title,
            'description': _text(item, 'description') or _text(item, 'content:encoded') or '',
            'link': _text(item, 'link'),
            'filename': filename,
            'pubDate': pub_date,
            'filesize': os.path.getsize(os.path.join(MEDIA_DIR, filename)),
            'explicit': (_text(item, 'itunes:explicit') or '').lower() in ('true', 'yes'),
        }

        ep_num = _text(item, 'podcast:episode') or _text(item, 'itunes:episode')
        if ep_num and ep_num.isdigit():
            episode['episodeNumber'] = int(ep_num)
        season = _text(item, 'podcast:season') or _text(item, 'itunes:season')
        if season and season.isdigit():
            episode['season'] = int(season)
        for field, path in (('episodeType', 'itunes:episodeType'),
                            ('duration', 'itunes:duration'),
                            ('subtitle', 'itunes:subtitle')):
            val = _text(item, path)
            if val:
                episode[field] = val

        art = _attr(item, 'itunes:image', 'href')
        if art:
            name = _localize(art, log)
            episode['artwork'] = f'{media_base_url}/{name}' if name else art

        t_url = _attr(item, 'podcast:transcript', 'url')
        if t_url:
            name = _localize(t_url, log)
            if name:
                episode['transcript'] = name

        ch_url = _attr(item, 'podcast:chapters', 'url')
        chapters = None
        if ch_url:
            name = _localize(ch_url, log)
            if name:
                chapters = _parse_chapters(name, media_base_url, log)
        if not chapters:
            chapters = _psc_to_chapters(item, media_base_url, log)
        if chapters:
            episode['chapters'] = chapters

        ep_loc = _parse_location(item.find('podcast:location', NS))
        if ep_loc:
            episode['location'] = ep_loc

        episode = {k: v for k, v in episode.items() if v is not None}
        data['episodes'].append(episode)
        existing_guids.add(ep_guid)
        added += 1
        log(color(f'    adopted: {title}', '32'))

    data['show'].update(show)
    data['episodes'].sort(key=store.episode_date, reverse=True)
    store.save(data)

    import feed as feedgen
    live, scheduled = feedgen.write_feed()

    log(f'\n  Adopted {added} episodes ({skipped_dup} already present, '
        f'{skipped_missing} skipped), feed.xml regenerated ({live} live).')
    log(f'  podcast:guid preserved: {show.get("guid", "(none in mirror)")}')
    log('  podcast:locked is now "no" -- update your feed URL in Apple Podcasts')
    log('  Connect, Spotify, and podcastindex.org to complete the move.')
    if show.get('value'):
        bad = [r for r in show['value'].get('recipients', [])
               if r.get('address') in (None, '', 'null')]
        if bad:
            log('  NOTE: your podcast:value block has a recipient with no valid')
            log('  node address -- edit "value" in podcast.json to receive payments.')

    return {'added': added, 'skipped_dup': skipped_dup, 'skipped_missing': skipped_missing}


if __name__ == '__main__':
    import sys
    try:
        promote_mirror()
    except Exception as e:
        print(f'promote failed: {e}', file=sys.stderr)
        sys.exit(1)
