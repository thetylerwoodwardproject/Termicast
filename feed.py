"""
feed.py - RSS feed generator

Strategy: build XML via string building with explicit CDATA for HTML content fields.
All non-HTML fields are passed through xml.sax.saxutils.escape() so special chars
are always properly encoded. CDATA blocks are used for description, content:encoded,
and itunes:summary so HTML markup is preserved exactly as authored.
"""
import json
import os
import re
import xml.sax.saxutils as saxutils
from datetime import datetime, timezone
from email.utils import formatdate

import store

BASE_DIR = os.environ.get('TERMICAST_DATA_DIR') or os.path.dirname(__file__)
FEED_FILE = os.path.join(BASE_DIR, 'feed.xml')
MEDIA_DIR = os.path.join(BASE_DIR, 'media')

MIME_MAP = {
    'mp3': 'audio/mpeg',
    'm4a': 'audio/x-m4a',
    'ogg': 'audio/ogg',
    'opus': 'audio/opus',
    'wav': 'audio/wav',
}

TRANSCRIPT_MIME = {
    'srt': 'application/x-subrip',
    'vtt': 'text/vtt',
    'txt': 'text/plain',
    'json': 'application/json',
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def x(s):
    """Escape a plain-text value for safe inclusion in XML."""
    if s is None:
        return ''
    return saxutils.escape(str(s))


def xa(s):
    """Escape a value for use inside an XML attribute."""
    if s is None:
        return ''
    return saxutils.escape(str(s), {'"': '&quot;'})


def cdata(s):
    """Wrap a string in a CDATA section. Handles nested ]]> by splitting it."""
    if s is None:
        s = ''
    # CDATA cannot contain ]]> -- split it if present
    s = str(s).replace(']]>', ']]]]><![CDATA[>')
    return f'<![CDATA[{s}]]>'


def strip_html(html):
    """Strip HTML tags for plain-text fallback fields like itunes:summary."""
    if not html:
        return ''
    # Add a newline before block-level tags so text doesn't run together
    block_tags = ['p', 'br', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'div', 'tr']
    text = str(html)
    for tag in block_tags:
        text = re.sub(rf'</?{tag}[^>]*>', '\n', text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r'<[^>]+>', '', text)
    # Decode common entities
    text = (text
        .replace('&amp;', '&')
        .replace('&lt;', '<')
        .replace('&gt;', '>')
        .replace('&nbsp;', ' ')
        .replace('&mdash;', '--')
        .replace('&ndash;', '-')
        .replace('&quot;', '"')
        .replace('&#38;', '&')
        .replace('&#39;', "'")
    )
    # Collapse multiple blank lines to a single newline, strip leading/trailing whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def to_rfc2822(date_str):
    dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return formatdate(dt.timestamp(), usegmt=True)


def parse_pubdate(date_str):
    dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def audio_mime(filename):
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    return MIME_MAP.get(ext, 'audio/mpeg')


def transcript_mime(filename):
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    return TRANSCRIPT_MIME.get(ext, 'text/plain')


def location_attrs(loc):
    """Build the attribute string for a podcast:location tag."""
    attrs = ''
    for key in ('rel', 'geo', 'osm', 'country'):
        if loc.get(key):
            attrs += f' {key}="{xa(loc[key])}"'
    return attrs


def chapters_json_path(ep_id):
    """Path to the generated podcast:chapters JSON file for an episode."""
    return os.path.join(MEDIA_DIR, f'{ep_id}-chapters.json')


def build_chapters_json(chapters):
    if not chapters:
        return None
    result = {'version': '1.2.0', 'chapters': []}
    for ch in chapters:
        obj = {'startTime': ch['startTime'], 'title': ch['title']}
        if ch.get('url'):
            obj['url'] = ch['url']
        if ch.get('img'):
            obj['img'] = ch['img']
        if ch.get('toc') is False:
            obj['toc'] = False
        result['chapters'].append(obj)
    return result


# ── Feed generator ────────────────────────────────────────────────────────────

def generate_feed():
    show = store.get_show()
    all_episodes = store.get_episodes()
    now = datetime.now(timezone.utc)

    episodes = [e for e in all_episodes if parse_pubdate(e['pubDate']) <= now]

    base_url = show['baseUrl'].rstrip('/')
    op3 = show.get('op3Prefix', 'https://op3.dev/e/').rstrip('/') + '/'

    L = []  # output lines

    L.append('<?xml version="1.0" encoding="UTF-8"?>')
    L.append('<rss version="2.0"')
    L.append('  xmlns:dc="http://purl.org/dc/elements/1.1/"')
    L.append('  xmlns:content="http://purl.org/rss/1.0/modules/content/"')
    L.append('  xmlns:atom="http://www.w3.org/2005/Atom"')
    L.append('  xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"')
    L.append('  xmlns:podcast="https://podcastindex.org/namespace/1.0">')
    L.append('<channel>')

    # ── Channel metadata ──────────────────────────────────────────────────────
    L.append(f'  <title>{cdata(show.get("title", ""))}</title>')
    L.append(f'  <description>{cdata(show.get("description", ""))}</description>')
    L.append(f'  <link>{x(show.get("link", ""))}</link>')
    L.append(f'  <language>{x(show.get("language", "en"))}</language>')
    L.append(f'  <atom:link href="{xa(base_url + "/feed.xml")}" rel="self" type="application/rss+xml"/>')

    if show.get('author'):
        L.append(f'  <author>{cdata(show["author"])}</author>')
        L.append(f'  <itunes:author>{x(show["author"])}</itunes:author>')

    if show.get('subtitle'):
        L.append(f'  <itunes:subtitle>{x(strip_html(show["subtitle"]))}</itunes:subtitle>')

    L.append(f'  <itunes:type>episodic</itunes:type>')
    L.append(f'  <itunes:explicit>{"true" if show.get("explicit") else "false"}</itunes:explicit>')

    if show.get('description'):
        L.append(f'  <itunes:summary>{cdata(show["description"])}</itunes:summary>')

    if show.get('email') or show.get('author'):
        L.append('  <itunes:owner>')
        L.append(f'    <itunes:name>{x(show.get("author", ""))}</itunes:name>')
        if show.get('email'):
            L.append(f'    <itunes:email>{x(show["email"])}</itunes:email>')
        L.append('  </itunes:owner>')

    if show.get('artwork'):
        L.append(f'  <itunes:image href="{xa(show["artwork"])}"/>')
        L.append('  <image>')
        L.append(f'    <url>{x(show["artwork"])}</url>')
        L.append(f'    <title>{x(show.get("title", ""))}</title>')
        L.append(f'    <link>{x(show.get("link", ""))}</link>')
        L.append('  </image>')

    # Categories: a 'categories' list supports multiple entries (as adopted
    # from a mirrored feed); the single category/subcategory pair is the
    # original wizard-driven config.
    cats = show.get('categories') or []
    if not cats and show.get('category'):
        cats = [{'category': show['category'], 'subcategory': show.get('subcategory')}]
    for c in cats:
        if c.get('subcategory'):
            L.append(f'  <itunes:category text="{xa(c["category"])}">')
            L.append(f'    <itunes:category text="{xa(c["subcategory"])}"/>')
            L.append('  </itunes:category>')
        else:
            L.append(f'  <itunes:category text="{xa(c["category"])}"/>')

    if show.get('copyright'):
        L.append(f'  <copyright>{cdata(show["copyright"])}</copyright>')

    locked_owner = f' owner="{xa(show["lockedOwner"])}"' if show.get('lockedOwner') else ''
    L.append(f'  <podcast:locked{locked_owner}>{x(show.get("locked", "no"))}</podcast:locked>')
    if show.get('guid'):
        L.append(f'  <podcast:guid>{x(show["guid"])}</podcast:guid>')

    if show.get('funding'):
        f = show['funding']
        L.append(f'  <podcast:funding url="{xa(f.get("url", ""))}">{x(f.get("text", ""))}</podcast:funding>')

    if show.get('license'):
        L.append(f'  <podcast:license>{x(show["license"])}</podcast:license>')

    if show.get('medium'):
        L.append(f'  <podcast:medium>{x(show["medium"])}</podcast:medium>')

    if show.get('location'):
        loc = show['location']
        L.append(f'  <podcast:location{location_attrs(loc)}>{x(loc.get("name", ""))}</podcast:location>')

    if show.get('value'):
        v = show['value']
        suggested = f' suggested="{xa(v["suggested"])}"' if v.get('suggested') else ''
        L.append(f'  <podcast:value type="{xa(v.get("type", ""))}" method="{xa(v.get("method", ""))}"{suggested}>')
        for r in v.get('recipients', []):
            attrs = ''
            for key in ('name', 'type', 'address', 'split', 'customKey', 'customValue', 'fee'):
                if r.get(key) is not None:
                    attrs += f' {key}="{xa(r[key])}"'
            L.append(f'    <podcast:valueRecipient{attrs}/>')
        L.append('  </podcast:value>')

    if show.get('podroll'):
        L.append('  <podcast:podroll>')
        for item in show['podroll']:
            attrs = ''
            for key in ('feedGuid', 'feedUrl', 'itemGuid'):
                if item.get(key):
                    attrs += f' {key}="{xa(item[key])}"'
            L.append(f'    <podcast:remoteItem{attrs}/>')
        L.append('  </podcast:podroll>')

    # ── Episodes ──────────────────────────────────────────────────────────────
    for ep in episodes:
        audio_url = f'{base_url}/media/{ep["filename"]}'
        op3_url = f'{op3}{audio_url}'
        mime = audio_mime(ep['filename'])

        # description may be HTML or plain text -- always wrap in CDATA
        description_html = ep.get('description', '')
        description_plain = strip_html(description_html)

        L.append('  <item>')
        L.append(f'    <title>{cdata(ep["title"])}</title>')
        L.append(f'    <link>{x(ep.get("link", ""))}</link>')
        L.append(f'    <guid isPermaLink="false">{x(ep["guid"])}</guid>')
        L.append(f'    <pubDate>{to_rfc2822(ep["pubDate"])}</pubDate>')

        if ep.get('author'):
            L.append(f'    <dc:creator>{cdata(ep["author"])}</dc:creator>')
        elif show.get('author'):
            L.append(f'    <dc:creator>{cdata(show["author"])}</dc:creator>')

        # Plain text description for basic RSS clients
        L.append(f'    <description>{cdata(description_html)}</description>')

        # Full HTML in content:encoded for clients that support it
        L.append(f'    <content:encoded>{cdata(description_html)}</content:encoded>')

        L.append(f'    <enclosure url="{xa(op3_url)}" length="{ep.get("filesize", 0)}" type="{mime}"/>')

        L.append(f'    <itunes:author>{x(ep.get("author") or show.get("author", ""))}</itunes:author>')
        L.append(f'    <itunes:explicit>{"true" if ep.get("explicit") else "false"}</itunes:explicit>')

        if ep.get('duration') is not None:
            L.append(f'    <itunes:duration>{x(ep["duration"])}</itunes:duration>')

        if ep.get('artwork'):
            L.append(f'    <itunes:image href="{xa(ep["artwork"])}"/>')

        if ep.get('episodeNumber'):
            L.append(f'    <itunes:episode>{ep["episodeNumber"]}</itunes:episode>')

        if ep.get('season'):
            L.append(f'    <itunes:season>{ep["season"]}</itunes:season>')

        if ep.get('episodeType'):
            L.append(f'    <itunes:episodeType>{x(ep["episodeType"])}</itunes:episodeType>')

        if ep.get('subtitle'):
            L.append(f'    <itunes:subtitle>{x(strip_html(ep["subtitle"]))}</itunes:subtitle>')

        # itunes:summary as plain text (spec says no HTML here)
        L.append(f'    <itunes:summary>{x(description_plain)}</itunes:summary>')

        # ── Podcast 2.0 tags ─────────────────────────────────────────────────

        if ep.get('transcript'):
            t_url = f'{base_url}/media/{ep["transcript"]}'
            L.append(f'    <podcast:transcript url="{xa(t_url)}" type="{transcript_mime(ep["transcript"])}"/>')

        if ep.get('chapters'):
            chap_url = f'{base_url}/media/{ep["id"]}-chapters.json'
            L.append(f'    <podcast:chapters url="{xa(chap_url)}" type="application/json+chapters"/>')
            # Write chapters JSON file to disk
            chap_file = chapters_json_path(ep['id'])
            os.makedirs(os.path.dirname(chap_file), exist_ok=True)
            with open(chap_file, 'w', encoding='utf-8') as f:
                json.dump(build_chapters_json(ep['chapters']), f, indent=2)

        if ep.get('soundbite'):
            sb = ep['soundbite']
            title_attr = f' title="{xa(sb["title"])}"' if sb.get('title') else ''
            L.append(f'    <podcast:soundbite startTime="{sb["startTime"]}" duration="{sb["duration"]}"{title_attr}/>')

        if ep.get('persons'):
            for p in ep['persons']:
                attrs = ''
                if p.get('role'):
                    attrs += f' role="{xa(p["role"])}"'
                if p.get('group'):
                    attrs += f' group="{xa(p["group"])}"'
                if p.get('img'):
                    attrs += f' img="{xa(p["img"])}"'
                if p.get('href'):
                    attrs += f' href="{xa(p["href"])}"'
                L.append(f'    <podcast:person{attrs}>{x(p["name"])}</podcast:person>')

        if ep.get('location'):
            loc = ep['location']
            L.append(f'    <podcast:location{location_attrs(loc)}>{x(loc["name"])}</podcast:location>')

        if ep.get('season'):
            L.append(f'    <podcast:season>{ep["season"]}</podcast:season>')
        if ep.get('episodeNumber'):
            L.append(f'    <podcast:episode>{ep["episodeNumber"]}</podcast:episode>')

        L.append('  </item>')

    L.append('</channel>')
    L.append('</rss>')

    return '\n'.join(L)


def write_feed():
    """Generate feed.xml and write it to disk. Returns (live_count, scheduled_count)."""
    xml = generate_feed()
    with open(FEED_FILE, 'w', encoding='utf-8') as f:
        f.write(xml)

    all_episodes = store.get_episodes()
    now = datetime.now(timezone.utc)
    live = sum(1 for e in all_episodes if parse_pubdate(e['pubDate']) <= now)
    scheduled = len(all_episodes) - live
    return live, scheduled
